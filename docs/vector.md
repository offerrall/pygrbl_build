# Vector

## SVG vector (`svg_gcode` + `SvgProfile`)

```python
from pygrbl_build import SvgProfile, svg_gcode, write_gcode

profile = SvgProfile(feed=1000, s_max=255)
write_gcode(svg_gcode("logo.svg", profile), "logo.nc")
```

`svg_gcode` accepts a path as before, or the SVG XML directly as `str`, `bytes`,
or `bytearray`.

`SvgProfile`'s defaults reproduce LaserGRBL's own SVG-import defaults, so
the output matches the desktop app for the same drawing. `text` and
`image` elements are skipped — convert text to paths in your editor
first.

## Image vector (`img2vector_gcode` + `Img2VectorProfile`)

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

## Image to SVG (`img2svg` + `Img2SvgProfile`)

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
