# Bounds and framing

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
