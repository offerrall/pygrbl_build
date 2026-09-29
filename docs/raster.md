# Raster

Each algorithm pairs a `*_gcode` generator with its own `*Profile`
config, so adding one never touches the others.

## Line-to-Line (`l2l_gcode` + `L2LProfile`)

```python
from pygrbl_build import L2LProfile, l2l_gcode, write_gcode

profile = L2LProfile(width_mm=300.0, lines_per_mm=10.0, feed=3000, s_max=100)
write_gcode(l2l_gcode("shield.png", profile), "shield.nc")
```

## Jarvis dithering (`jarvis_gcode` + `JarvisProfile`)

```python
from pygrbl_build import JarvisProfile, jarvis_gcode, write_gcode

profile = JarvisProfile(width_mm=80.0, lines_per_mm=3.0, feed=3000, s_max=1000)
write_gcode(jarvis_gcode("photo.png", profile), "photo.nc")
```

Jarvis converts a color or gray image to a black-and-white dot pattern.
Its profile exposes LaserGRBL's grayscale formula, channel weights,
brightness, contrast and white clip, plus this library's bidirectional
scan and optional overscan. It uses horizontal raster passes; vertical
and diagonal directions are not implemented. The diffusion follows the
coefficients and edge behavior of LaserGRBL's Jarvis implementation.

## Image inputs

All image APIs (`l2l_gcode`, `jarvis_gcode`, `img2vector_gcode`, and `img2svg`) also accept
encoded image `bytes`, `bytearray`, or an already loaded `PIL.Image.Image`.
This allows in-memory services to work without writing a temporary image:

```python
from PIL import Image

with open("shield.png", "rb") as source:
    from_bytes = l2l_gcode(source.read(), profile)

from_pillow = l2l_gcode(Image.open("shield.png"), profile)
```
