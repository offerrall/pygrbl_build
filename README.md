# PyGrbl_Build 0.3.0

[![PyPI](https://img.shields.io/pypi/v/pygrbl_build.svg)](https://pypi.org/project/pygrbl_build/)

A collection of algorithms to generate **G-code for GRBL diode lasers**
from different sources, plus tooling around the G-code itself. Four
generators today:

- **Line-to-Line** (`l2l_gcode`) — raster engraving from an image, with
  LaserGRBL fidelity.
- **SVG vector** (`svg_gcode`) — vector tracing from an SVG (paths,
  basic shapes, groups, transforms), a faithful port of LaserGRBL's SVG
  import. Pure Python, no extra dependency.
- **Image vector** (`img2vector_gcode`) — outline tracing from a raster
  image (LaserGRBL's "Vectorize!"): the image is reduced to black/white,
  Potrace traces its outlines as closed contours, and each curve is
  emitted as G2/G3 arcs. Pure Python, Pillow only. Outlines only today
  (no interior filling yet).
- **Image to SVG** (`img2svg`) — the same Potrace trace as
  `img2vector_gcode`, but the contours are written to a standard vector
  SVG instead of G-code. Inner contours become holes (`fill-rule`
  `evenodd`), so you get the filled black silhouette potrace.exe produces.
  Pure Python, Pillow only.

Plus **G-code bounds & framing** (`get_bounding_box` +
`generate_framing_gcode`): a fast C parser for the bounding box of any
G-code (file or in-memory) and a framing pass that traces it, so the
operator can confirm placement before engraving.

Part of the **pygrbl** family, a set of libraries to manage GRBL.
Companion to [`pygrbl_streamer`](https://github.com/offerrall/pygrbl_streamer)

## Speed

This is the whole point. A full 300 mm @ 10 lines/mm raster job — nearly
**4.7 million lines** of G-code — comes out in **~0.34 s**. LaserGRBL can
take around **2 minutes** to produce the same job: that's roughly a
**350× speedup**, and byte-for-byte the same output.

## Install

```
pip install pygrbl-build
```

**The only requirements are Pillow and a C compiler.** Pillow is the
single Python dependency (image loading and resizing); the C compiler is
needed at install time because the raster engine ships as a C extension.
Nothing else — no numpy, no runtime toolchain.

## Usage

Each algorithm pairs a `*_gcode` generator with its own `*Profile`
config, so adding one never touches the others.

Raster Line-to-Line (`l2l_gcode` + `L2LProfile`):

```python
from pygrbl_build import L2LProfile, l2l_gcode, write_gcode

profile = L2LProfile(width_mm=300.0, lines_per_mm=10.0, feed=3000, s_max=100)
write_gcode(l2l_gcode("shield.png", profile), "shield.nc")
```

SVG vector (`svg_gcode` + `SvgProfile`):

```python
from pygrbl_build import SvgProfile, svg_gcode, write_gcode

profile = SvgProfile(feed=1000, s_max=255)
write_gcode(svg_gcode("logo.svg", profile), "logo.nc")
```

`SvgProfile`'s defaults reproduce LaserGRBL's own SVG-import defaults, so
the output matches the desktop app for the same drawing. `text` and
`image` elements are skipped — convert text to paths in your editor
first.

Image vector (`img2vector_gcode` + `Img2VectorProfile`):

```python
from pygrbl_build import Img2VectorProfile, img2vector_gcode, write_gcode

profile = Img2VectorProfile(width_mm=80.0, quality=10.0, feed=1000, s_max=1000)
write_gcode(img2vector_gcode("logo.png", profile), "logo.nc")
```

`img2vector_gcode` is a faithful port of LaserGRBL's "Vectorize!": the
image is reduced to black/white (resize, grayscale, white-clip, optional
threshold), Potrace traces its outlines, and each cubic Bezier is
approximated by biarcs and emitted as `G2`/`G3` arcs (with a `G1`
fallback). `width_mm` sets the physical width and `quality` the tracing
resolution in pixels/mm. The `Img2VectorProfile` defaults follow Potrace's
classic settings (smooth curves, optimization on); set `alphamax=0.0` and
`opticurve=False` to mimic LaserGRBL's own out-of-the-box UI defaults.

Image to SVG (`img2svg` + `Img2SvgProfile`):

```python
from pygrbl_build import Img2SvgProfile, img2svg

profile = Img2SvgProfile(width_mm=80.0, quality=10.0)
svg = img2svg("logo.png", profile)
with open("logo.svg", "w", encoding="utf-8") as f:
    f.write(svg)
```

`img2svg` runs the same trace as `img2vector_gcode` (resize, grayscale,
white-clip, optional threshold, then Potrace outlines), but skips the
biarc/G-code stages and writes the contours as a single filled `<path>`.
It returns the complete SVG document as a string (not a G-code iterator,
so use your own `open()`). The `Img2SvgProfile` carries only the tracing
and binarization knobs — no feed, power or laser-mode fields. `viewBox`
is in pixels (`width_mm*quality`) while `width`/`height` carry the
physical size in mm, and the image keeps its natural top-down
orientation (no Y-flip, unlike the G-code path).

G-code bounds & framing (`get_bounding_box` + `generate_framing_gcode`):

```python
from pygrbl_build import get_bounding_box, generate_framing_gcode

# From a file path (opened and streamed in C — handles 500MB+ in seconds)...
min_x, max_x, min_y, max_y = get_bounding_box("job.nc")

# ...or straight from G-code already in memory, no file needed:
gcode = "\n".join(svg_gcode("logo.svg", SvgProfile()))
min_x, max_x, min_y, max_y = get_bounding_box(gcode)          # str
min_x, max_x, min_y, max_y = get_bounding_box(gcode.encode()) # or bytes

frame = generate_framing_gcode(min_x, max_x, min_y, max_y, power=10.0, speed=1000)
```

`get_bounding_box` is the original [gcode-bounds](https://github.com/offerrall/gcode-bounds)
C parser folded in. It accepts a file path (`str`/`Path`, opened and
streamed in C) or the G-code content directly (`bytes`, or a multi-line
`str`), so it never has to exist on disk — the Python wrapper picks the
route. Only X/Y are considered; rapid moves to the origin (`G0` with
`X0`/`Y0`) are skipped so home moves don't expand the box.
`generate_framing_gcode` returns the perimeter trace as a list of lines
(`power` is 0-100, `speed` in mm/min).

`write_gcode` writes the path verbatim, so you choose the extension
(`.nc`, `.gcode`, `.g`, ...). It's just a convenience: every `*_gcode`
generator is a lazy iterator of lines, so anything beyond writing a
plain file (compression, network shipping, streaming to the machine) is
the upper layer's job — consume the iterator with whatever sink you need.

Public API: `L2LProfile`, `l2l_gcode`, `SvgProfile`, `svg_gcode`,
`Img2VectorProfile`, `img2vector_gcode`, `Img2SvgProfile`, `img2svg`,
`get_bounding_box`, `generate_framing_gcode`, `write_gcode`. See the
docstrings.
