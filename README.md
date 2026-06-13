# PyGrbl_Build 0.0.1

[![PyPI](https://img.shields.io/pypi/v/pygrbl_build.svg)](https://pypi.org/project/pygrbl_build/)

A collection of algorithms to generate **G-code for GRBL diode lasers**
from different sources. Two algorithms today:

- **Line-to-Line** (`l2l_gcode`) — raster engraving from an image, with
  LaserGRBL fidelity.
- **SVG vector** (`svg_gcode`) — vector tracing from an SVG (paths,
  basic shapes, groups, transforms), a faithful port of LaserGRBL's SVG
  import. Pure Python, no extra dependency.

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

`write_gcode` writes the path verbatim, so you choose the extension
(`.nc`, `.gcode`, `.g`, ...). It's just a convenience: every `*_gcode`
generator is a lazy iterator of lines, so anything beyond writing a
plain file (compression, network shipping, streaming to the machine) is
the upper layer's job — consume the iterator with whatever sink you need.

Public API: `L2LProfile`, `l2l_gcode`, `SvgProfile`, `svg_gcode`,
`write_gcode`. See the docstrings.
