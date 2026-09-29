# Overview

pygrbl-build is a collection of algorithms that turn images and SVGs into
G-code for GRBL diode lasers, plus tooling around the G-code itself. Each
algorithm pairs a generator function with its own frozen, validated `*Profile`
configuration, so adding one never touches the others.

## Algorithms

| Algorithm | Function and profile | What it does |
|---|---|---|
| Line-to-Line | `l2l_gcode` + `L2LProfile` | Raster engraving of a grayscale image, with LaserGRBL fidelity. |
| Jarvis | `jarvis_gcode` + `JarvisProfile` | 1-bit Jarvis-Judice-Ninke error diffusion, engraved as short powered raster segments, as in LaserGRBL. |
| SVG vector | `svg_gcode` + `SvgProfile` | Vector tracing of an SVG (paths, basic shapes, groups, transforms), a port of LaserGRBL's SVG import. |
| Image vector | `img2vector_gcode` + `Img2VectorProfile` | Outline tracing of a raster image (LaserGRBL's "Vectorize!"): Potrace contours emitted as `G2`/`G3` arcs. |
| Image to SVG | `img2svg` + `Img2SvgProfile` | The same Potrace trace, written as a standard SVG with filled silhouettes instead of G-code. |
| Bounds and framing | `get_bounding_box` + `generate_framing_gcode` | The bounding box of any G-code and a framing pass that traces it, so the operator can confirm placement before engraving. |

[Raster](raster.md), [Vector](vector.md) and [Bounds and framing](framing.md)
cover each one in detail.

## Speed

A full 300 mm raster job at 10 lines/mm, nearly **4.7 million lines** of
G-code, comes out of `l2l_gcode` in **about 0.34 s**. LaserGRBL takes around
**2 minutes** to produce the same job, byte for byte: roughly a **350×
speedup**. The raster engine is a C extension; the vector algorithms are pure
Python.

## Lazy output

Every `*_gcode` generator is a lazy iterator of G-code lines without trailing
newlines: the image or SVG loads at call time, and lines are produced as they
are consumed. The full G-code never needs to exist in memory or on disk.
Every output starts with a comment header naming the library version, the
source and its hash, and the profile, so any engraved piece can be traced
back to its exact recipe.

`write_gcode(lines, path)` writes the lines to a plain text file, batched, and
returns the number of lines written. The path is written verbatim, so the
caller chooses the extension (`.nc`, `.gcode`, `.g`, ...). Anything beyond a
plain file, such as compression, network shipping or streaming to the
machine, is the caller's job: consume the iterator with the sink it needs.

With [pygrbl-streamer](https://offerrall.github.io/pygrbl-streamer/), the
lines go straight to the machine:

```python
from pygrbl_build import L2LProfile, l2l_gcode
from pygrbl_streamer import GrblStreamer

profile = L2LProfile(width_mm=300.0, lines_per_mm=10.0, feed=3000, s_max=100)

with GrblStreamer("/dev/ttyUSB0") as laser:
    if not laser.stream(l2l_gcode("shield.png", profile)):
        raise RuntimeError("Job did not complete")
```

Every public function and profile field is documented in its docstring.
