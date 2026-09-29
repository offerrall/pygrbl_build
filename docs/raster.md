# Raster

Raster algorithms engrave an image row by row. Output uses absolute
coordinates (`G90`) in millimeters (`G21`), with the origin at the engraving's
bottom-left and Y growing upward. `width_mm` sets the physical width; the
height follows the image's aspect ratio. `laser_on` is `"M4"` (dynamic power,
requires `$32=1`) by default, or `"M3"` for constant power.

## Line-to-Line (`l2l_gcode` + `L2LProfile`)

Line-to-Line maps each gray level to a laser power between `s_min` and
`s_max`, in a serpentine scan with run-length-encoded power segments; fully
blank rows are skipped. It accepts grayscale images only: a color image raises
`ValueError`, with LaserGRBL's own test, so convert it in an image editor
first. Transparency is honored: transparent pixels are blank.

```python
from pygrbl_build import L2LProfile, l2l_gcode, write_gcode

profile = L2LProfile(
    width_mm=120.0,
    lines_per_mm=10.0,       # LaserGRBL's "Quality"; 10 lines/mm is about 254 DPI
    feed=3000,               # mm/min
    s_min=0,                 # power for the lightest non-white gray
    s_max=1000,              # power for pure black, relative to $30
    white_threshold=250,     # gray at or above this is white; 250 is LaserGRBL's WhiteClip=5
    overscan_mm=2.0,         # beam-off travel past row ends; 0 is LaserGRBL-faithful
    bidirectional=True,      # False always scans left to right
    invert=False,            # True engraves the negative
)
write_gcode(l2l_gcode("portrait.png", profile), "portrait.nc")
```

A profile is frozen and validated at construction: a bad value raises
`TypeError` or `ValueError` there, never in the G-code.

## Jarvis dithering (`jarvis_gcode` + `JarvisProfile`)

```python
from pygrbl_build import JarvisProfile, jarvis_gcode, write_gcode

profile = JarvisProfile(width_mm=80.0, lines_per_mm=3.0, feed=3000, s_max=1000)
write_gcode(jarvis_gcode("photo.png", profile), "photo.nc")
```

Jarvis converts a color or gray image to a black-and-white dot pattern with
Jarvis-Judice-Ninke error diffusion. Dots are engraved at `s_max` during
horizontal raster moves; white pixels use `S0`, and `lines_per_mm` sets the dot
pitch. The profile exposes LaserGRBL's grayscale formula, channel weights,
brightness, contrast and white clip, plus this library's bidirectional scan and
optional overscan. The diffusion follows the coefficients and edge behavior of
LaserGRBL's Jarvis implementation.

## Image inputs

Every image function (`l2l_gcode`, `jarvis_gcode`, `img2vector_gcode` and
`img2svg`) accepts a file path, encoded image `bytes` or `bytearray`, or an
already loaded `PIL.Image.Image`. In-memory services work without writing a
temporary image:

```python
from PIL import Image

from pygrbl_build import L2LProfile, l2l_gcode

profile = L2LProfile(width_mm=80.0)

with open("shield.png", "rb") as source:
    from_bytes = l2l_gcode(source.read(), profile)

from_pillow = l2l_gcode(Image.open("shield.png"), profile)
```

In-memory inputs get the same traceability header as paths: encoded bytes are
hashed directly, and Pillow images are hashed from their mode, size and pixel
content. The `ImageSource` type alias describes every accepted input.
