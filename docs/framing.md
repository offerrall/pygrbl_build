# Bounds and framing

```python
from pygrbl_build import SvgProfile, generate_framing_gcode, get_bounding_box, svg_gcode

# From a file path, opened and streamed in C...
min_x, max_x, min_y, max_y = get_bounding_box("job.nc")

# ...or from G-code already in memory, no file needed:
gcode = "\n".join(svg_gcode("logo.svg", SvgProfile()))
min_x, max_x, min_y, max_y = get_bounding_box(gcode)           # str
min_x, max_x, min_y, max_y = get_bounding_box(gcode.encode())  # or bytes

frame = generate_framing_gcode(min_x, max_x, min_y, max_y, power=10.0, speed=1000)
```

`get_bounding_box` returns `(min_x, max_x, min_y, max_y)`, computed by a C
parser. It accepts a file path (`str` or `Path`, opened and streamed in C) or
the G-code content itself (`bytes`, `bytearray` or a multi-line `str`), so the
G-code never has to exist on disk. A `str` is a path when it names an existing
file, and G-code text otherwise. Only X and Y are considered (Z is ignored),
and rapid moves to the origin (`G0` with `X0` and `Y0`) are skipped so the
generators' own home moves don't expand the box. G-code without X/Y
coordinates raises `ValueError`.

`generate_framing_gcode` returns the perimeter trace as a list of lines
(`power` is 0 to 100, `speed` in mm/min), ending with the beam off and a rapid
move to the origin. Run it before the job so the operator can confirm where the
work lands on the material.
