# pygrbl-build

Generate G-code for GRBL diode lasers from images and SVGs: Line-to-Line
raster, Jarvis dithering, SVG vector, image outline tracing, plus image to SVG
and framing around any G-code.

The output follows LaserGRBL, byte for byte for Line-to-Line, and the C raster
engine generates in a fraction of a second a job that LaserGRBL takes minutes
to produce.

```python
from pygrbl_build import L2LProfile, l2l_gcode, write_gcode

profile = L2LProfile(width_mm=300.0, lines_per_mm=10.0, feed=3000, s_max=100)
write_gcode(l2l_gcode("shield.png", profile), "shield.nc")
```

The full documentation is at https://offerrall.github.io/pygrbl-build/.

## Documentation

- [Overview](docs/overview.md): the algorithms, speed, lazy output and how it pairs with pygrbl-streamer.
- [Raster](docs/raster.md): Line-to-Line and Jarvis engraving, and the image inputs they accept.
- [Vector](docs/vector.md): SVG to G-code, image to G-code outlines, and image to SVG.
- [Bounds and framing](docs/framing.md): the bounding box of any G-code, and a framing pass around it.
- [Limitations](docs/limitations.md): what each algorithm does not do, and the platforms with prebuilt wheels.

### Maintaining

- [Releasing](docs/releasing.md): CI, PyPI trusted publishing and the release steps.
