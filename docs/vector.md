# Vector

## SVG vector (`svg_gcode` + `SvgProfile`)

```python
from pygrbl_build import SvgProfile, svg_gcode, write_gcode

profile = SvgProfile(feed=1000, s_max=255)
write_gcode(svg_gcode("logo.svg", profile), "logo.nc")
```

`svg_gcode` is a port of LaserGRBL's SVG import: it parses paths, basic shapes
(rect, circle, ellipse, line, polyline, polygon) and nested groups, applies
their transforms, flattens curves to segments and emits the same G-code as the
desktop app, including its move comments and modal G-code compression. The
drawing is scaled by the SVG's width, height and viewBox and flipped so it
grows upward. `SvgProfile`'s defaults reproduce LaserGRBL's own SVG-import
defaults, so the output matches the desktop app for the same drawing.

The source is a path to an SVG file, or the SVG XML itself as `str`, `bytes` or
`bytearray`; the `SvgSource` type alias describes every accepted input.

## Image vector (`img2vector_gcode` + `Img2VectorProfile`)

```python
from pygrbl_build import Img2VectorProfile, img2vector_gcode, write_gcode

profile = Img2VectorProfile(width_mm=80.0, quality=10.0, feed=1000, s_max=1000)
write_gcode(img2vector_gcode("logo.png", profile), "logo.nc")
```

`img2vector_gcode` is a port of LaserGRBL's "Vectorize!": the image is reduced
to black and white (resize, grayscale, white clip, optional threshold), Potrace
traces its outlines as closed contours, and each cubic Bezier is approximated
by biarcs and emitted as `G2`/`G3` arcs, with a `G1` fallback. `width_mm` sets
the physical width and `quality` the tracing resolution in pixels/mm. The
`Img2VectorProfile` defaults follow Potrace's classic settings (smooth curves,
optimization on); set `alphamax=0.0` and `opticurve=False` to mimic LaserGRBL's
own out-of-the-box defaults.

## Image to SVG (`img2svg` + `Img2SvgProfile`)

```python
from pygrbl_build import Img2SvgProfile, img2svg

profile = Img2SvgProfile(width_mm=80.0, quality=10.0)
svg = img2svg("logo.png", profile)
with open("logo.svg", "w", encoding="utf-8") as f:
    f.write(svg)
```

`img2svg` runs the same trace as `img2vector_gcode`, but skips the biarc and
G-code stages and writes the contours as a single filled `<path>` with
`fill-rule="evenodd"`: inner contours become holes, giving the filled black
silhouette potrace.exe produces. It returns the complete SVG document as a
string, not a G-code iterator. `Img2SvgProfile` carries only the tracing and
binarization settings, with no feed, power or laser-mode fields. The `viewBox`
is in pixels (`width_mm * quality`) while `width` and `height` carry the
physical size in mm, and the image keeps its natural top-down orientation (no
Y flip, unlike the G-code output).
