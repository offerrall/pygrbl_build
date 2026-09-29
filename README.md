# pygrbl-build

[![PyPI](https://img.shields.io/pypi/v/pygrbl_build.svg)](https://pypi.org/project/pygrbl_build/)

A collection of algorithms to generate **G-code for GRBL diode lasers**
from different sources, plus tooling around the G-code itself. Five
generators today:

- **Line-to-Line** (`l2l_gcode`) — raster engraving from an image, with
  LaserGRBL fidelity.
- **Jarvis** (`jarvis_gcode`) — 1-bit Jarvis-Judice-Ninke error diffusion
  followed by the same raster G-code engine. The dots are short powered
  raster segments, as in LaserGRBL.
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
Companion to [`pygrbl-streamer`](https://github.com/offerrall/pygrbl_streamer) and [`pygrbl-server`](https://github.com/offerrall/pygrbl-server)

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

## Use

```python
from pygrbl_build import L2LProfile, l2l_gcode, write_gcode

profile = L2LProfile(width_mm=300.0, lines_per_mm=10.0, feed=3000, s_max=100)
write_gcode(l2l_gcode("shield.png", profile), "shield.nc")
```

## Documentation

- [Raster](docs/raster.md): Line-to-Line and Jarvis engraving, and the image inputs they accept.
- [Vector](docs/vector.md): SVG to G-code, image to G-code outlines, and image to SVG.
- [Bounds and framing](docs/framing.md): the bounding box of any G-code, and a framing pass around it.
- [Output](docs/output.md): writing G-code, lazy line iterators, and the public API.
- [Changelog](CHANGELOG.md)
- [Releasing](RELEASING.md)
