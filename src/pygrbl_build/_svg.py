"""SVG -> G-code engine, ported from LaserGRBL's vector converter.

This is a faithful Python port of LaserGRBL's `GCodeFromSVG` +
`gcodeRelated` (themselves descended from GRBL-Plotter). It parses the
SVG geometry (paths, basic shapes, groups, transforms), flattens curves
to line segments and emits the exact same G-code the desktop app would,
including its single-letter move comments and run-length G-code
compression. Pure Python, no GUI and no Windows dependency: the original
only needed `System.Windows.Media.Matrix`, which is reimplemented here as
``_Matrix``.

Public surface lives in ``pygrbl_build.__init__`` (``SvgProfile`` and
``svg_gcode``); this module is the engine and is considered private.

Coordinate model (LaserGRBL-faithful): SVG user units are converted to
millimetres (or inches), Y is flipped so the engraving grows upward, and
the SVG's viewBox/width/height drive the scale exactly as the desktop
app computes it.
"""

import math
import re
import xml.etree.ElementTree as ET

# px-per-unit factors, identical to LaserGRBL (96 DPI base).
_IN2PX = 96.0
_MM2PX = 96.0 / 25.4
_CM2PX = 96.0 / 2.54
_PT2PX = 96.0 / 72.0
_PC2PX = 12.0 * 96.0 / 72.0
_EM2PX = 150.0

_BEZIER_ACCURACY = 12  # legacy fixed-segment count per curve

# Path 'd' tokenizer: split before every ASCII letter EXCEPT lowercase
# 'e' (which is an exponent inside a number). Mirrors LaserGRBL's
# regex char-class subtraction [A-Za-z-[e]].
_DSPLIT = re.compile(r"(?=[A-Za-df-z])")

# One path argument (signed int/float with optional exponent), identical
# to the LaserGRBL matcher.
_ARG = re.compile(
    r"((\-|)\d+(\.\d+|)((((E|e)(\-|\+|))|\.)\d+|)|((\-|)\.\d+))"
)

_NAMED_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "lime": (0, 255, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
}


def _fmt_num(v):
    """Format a coordinate like LaserGRBL's "0.###": up to 3 decimals,
    trailing zeros stripped, whole numbers with no decimal point."""
    s = f"{v:.3f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def _fmt_pow(v):
    """Format an S/F numeric like .NET's default float ToString: integers
    print without a decimal point, fractions in general form."""
    if v == int(v):
        return str(int(v))
    return f"{v:g}"


def _to_px(text, ext=1.0):
    """Convert an SVG length string to pixels (LaserGRBL's ConvertToPixel).

    Unit detected only when it is NOT at index 0 (a number must precede
    it), matching the original. '%' resolves against ``ext``.
    """
    if text is None:
        return 0.0
    percent = False
    factor = 1.0
    if text.find("mm") > 0:
        factor = _MM2PX
    elif text.find("cm") > 0:
        factor = _CM2PX
    elif text.find("in") > 0:
        factor = _IN2PX
    elif text.find("pt") > 0:
        factor = _PT2PX
    elif text.find("pc") > 0:
        factor = _PC2PX
    elif text.find("em") > 0:
        factor = _EM2PX
    elif text.find("%") > 0:
        percent = True
    cleaned = (
        text.replace("pt", "").replace("pc", "").replace("mm", "")
        .replace("cm", "").replace("in", "").replace("em ", "")
        .replace("%", "").replace("px", "")
    )
    cleaned = cleaned.strip()
    if not cleaned:
        return 0.0
    try:
        value = float(cleaned)
    except ValueError:
        return 0.0
    if percent:
        return value * ext / 100.0
    return value * factor


def _parse_color(text):
    """Best-effort CSS color -> (r, g, b), or None if unparseable."""
    s = text.strip().lower()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            try:
                return tuple(int(c * 2, 16) for c in h)
            except ValueError:
                return None
        if len(h) == 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                return None
        return None
    if s.startswith("rgb"):
        nums = re.findall(r"\d+", s)
        if len(nums) >= 3:
            return (int(nums[0]), int(nums[1]), int(nums[2]))
        return None
    return _NAMED_COLORS.get(s)


def _local(tag):
    """Local name of a possibly namespaced ElementTree tag."""
    return tag.rsplit("}", 1)[-1]


def _children(elem, name):
    """Direct children with the given local tag name (namespace-agnostic)."""
    return [c for c in elem if isinstance(c.tag, str) and _local(c.tag) == name]


class _Matrix:
    """2x3 affine transform with WPF ``System.Windows.Media.Matrix``
    semantics (row vectors: v' = v * M). Reimplements just what the SVG
    converter used."""

    __slots__ = ("m11", "m12", "m21", "m22", "ox", "oy")

    def __init__(self, m11=1.0, m12=0.0, m21=0.0, m22=1.0, ox=0.0, oy=0.0):
        self.m11 = m11
        self.m12 = m12
        self.m21 = m21
        self.m22 = m22
        self.ox = ox
        self.oy = oy

    def copy(self):
        return _Matrix(self.m11, self.m12, self.m21, self.m22, self.ox, self.oy)

    @staticmethod
    def multiply(a, b):
        """a then b (v * a * b), matching WPF Matrix.Multiply(a, b)."""
        return _Matrix(
            a.m11 * b.m11 + a.m12 * b.m21,
            a.m11 * b.m12 + a.m12 * b.m22,
            a.m21 * b.m11 + a.m22 * b.m21,
            a.m21 * b.m12 + a.m22 * b.m22,
            a.ox * b.m11 + a.oy * b.m21 + b.ox,
            a.ox * b.m12 + a.oy * b.m22 + b.oy,
        )

    def scale(self, sx, sy):
        """Append a scale (this = this * S), like WPF Matrix.Scale."""
        self.m11 *= sx
        self.m21 *= sx
        self.ox *= sx
        self.m12 *= sy
        self.m22 *= sy
        self.oy *= sy

    def rotate_at(self, angle_deg, cx, cy):
        """Append a rotation about (cx, cy), like WPF Matrix.RotateAt."""
        rad = angle_deg * math.pi / 180.0
        cos = math.cos(rad)
        sin = math.sin(rad)
        rot = _Matrix(cos, sin, -sin, cos, 0.0, 0.0)
        t1 = _Matrix(1.0, 0.0, 0.0, 1.0, -cx, -cy)
        t2 = _Matrix(1.0, 0.0, 0.0, 1.0, cx, cy)
        r = _Matrix.multiply(_Matrix.multiply(t1, rot), t2)
        res = _Matrix.multiply(self, r)
        self.m11, self.m12, self.m21, self.m22 = res.m11, res.m12, res.m21, res.m22
        self.ox, self.oy = res.ox, res.oy

    def transform(self, x, y):
        return (
            x * self.m11 + y * self.m21 + self.ox,
            x * self.m12 + y * self.m22 + self.oy,
        )


# --- Bezier flattening (port of BezierTools.cs) --------------------------- #


def _tri_area(a, b, c):
    return abs(
        a[0] * b[1] + b[0] * c[1] + c[0] * a[1]
        - a[1] * b[0] - b[1] * c[0] - c[1] * a[0]
    ) / 2.0


def _split_curve(cp, t):
    """De Casteljau split at t; returns (left, right) control points."""
    degree = len(cp) - 1
    v = [[None] * (degree + 1) for _ in range(degree + 1)]
    for j in range(degree + 1):
        v[0][j] = cp[j]
    for i in range(1, degree + 1):
        for j in range(degree - i + 1):
            x = (1.0 - t) * v[i - 1][j][0] + t * v[i - 1][j + 1][0]
            y = (1.0 - t) * v[i - 1][j][1] + t * v[i - 1][j + 1][1]
            v[i][j] = (x, y)
    left = [v[i][0] for i in range(degree + 1)]
    right = [v[degree - i][i] for i in range(degree + 1)]
    return left, right


def _flatten_segment(seg, error, subdiv, max_subdiv):
    if subdiv >= max_subdiv or (
        math.sqrt(_tri_area(seg[0], seg[1], seg[2])) < error
        and math.sqrt(_tri_area(seg[1], seg[2], seg[3])) < error
    ):
        return list(seg)
    left, right = _split_curve(seg, 0.5)
    a = _flatten_segment(left[:4], error, subdiv + 1, max_subdiv)
    b = _flatten_segment(right[:4], error, subdiv + 1, max_subdiv)
    return a + b[1:]


def _flatten_to(points, error=0.01, max_subdiv=20):
    """Adaptive flatten of contiguous cubic segments to a polyline.

    Walks the control points in steps of 3 (cubics share endpoints), so
    a single 4-point curve runs exactly one iteration.
    """
    result = [points[0]]
    i = 0
    while i + 3 < len(points):
        result.extend(_flatten_segment(points[i:i + 4], error, 0, max_subdiv)[1:])
        i += 3
    return result


def _bezier_point(t, cp, index, count):
    if count == 1:
        return cp[index]
    p0 = _bezier_point(t, cp, index, count - 1)
    p1 = _bezier_point(t, cp, index + 1, count - 1)
    return ((1.0 - t) * p0[0] + t * p1[0], (1.0 - t) * p0[1] + t * p1[1])


def _bezier_old(cp, segments):
    return [
        _bezier_point(i / segments, cp, 0, len(cp)) for i in range(segments + 1)
    ]


# --- G-code emitter (port of the static `gcode` class) -------------------- #


class _Gcode:
    """Emits G-code lines (no trailing newline) into ``self.lines``,
    replicating LaserGRBL's gcodeRelated emitter: S-command PWM mode,
    run-length compression and modal G/feed/spindle tracking."""

    def __init__(self, profile):
        self.lines = []
        self.support_pwm = profile.support_pwm
        self.laser_on = profile.laser_on
        self.laser_off = profile.laser_off
        self.firmware = profile.firmware
        self.no_arcs = profile.no_arcs
        self.compress = True
        self.rapidnum = 0  # G0 for rapids ("Disable G0 fast skip" = off)

        spindle = float(profile.s_max)
        if self.firmware == "smoothie":
            spindle /= 255.0
        self.spindle = spindle
        self._spindle_str = _fmt_pow(spindle)
        self._feed = profile.feed
        self._feed_str = _fmt_pow(float(profile.feed))

        # modal state (mirrors gcode.setup())
        self.apply_xy_feed = True
        self.lastx = -1.0
        self.lasty = -1.0
        self.lastz = 0.0
        self.lasts = -1.0
        self.lastg = -1
        self.lastf = 0.0
        self.last_move_was_g0 = True
        self._seg_x = self._seg_y = 0.0

    # spindle / pen
    def spindle_on(self, cmt):
        c = f" ({cmt})" if cmt else ""
        if self.support_pwm:
            self.lines.append(f"S{self._spindle_str}{c}")
        else:
            self.lines.append(f"{self.laser_on}{c}")

    def spindle_off(self, cmt):
        c = f" ({cmt})" if cmt else ""
        if self.support_pwm:
            self.lines.append(f"S0{c}")
        else:
            self.lines.append(f"{self.laser_off}{c}")

    def pen_down(self, cmt):
        self.apply_xy_feed = True
        self.spindle_on(cmt)

    def pen_up(self, cmt):
        self.spindle_off(cmt)

    # moves
    def move_rapid(self, x, y, cmt):
        self._move(self.rapidnum, x, y, None, False, cmt)
        self.last_move_was_g0 = True

    def move_to(self, x, y, cmt):
        self._seg_x, self._seg_y = x, y
        self._move(1, x, y, None, self.apply_xy_feed, cmt)
        self.last_move_was_g0 = False

    def _move(self, gnr, x, y, z, apply_feed, cmt):
        if gnr == 0:
            self._seg_x, self._seg_y = x, y
        tz = self.lastz if z is None else z
        feed = ""
        if apply_feed and gnr > 0:
            feed = f"F{self._feed_str}"
            self.apply_xy_feed = False
        cmt = f"({cmt})" if cmt else ""

        if self.compress:
            if gnr > 0 or self.lastx != x or self.lasty != y or self.lastz != tz:
                parts = []
                needed = False
                if self.lastg != gnr:
                    parts.append(f"G{gnr}")
                    needed = True
                if self.lastx != x:
                    parts.append(f"X{_fmt_num(x)}")
                    needed = True
                if self.lasty != y:
                    parts.append(f"Y{_fmt_num(y)}")
                    needed = True
                if z is not None and self.lastz != z:
                    parts.append(f"Z{_fmt_num(z)}")
                    needed = True
                if (gnr == 1 and self.lastf != self._feed) or apply_feed:
                    parts.append(f"F{self._feed_str}")
                    self.lastf = self._feed
                    needed = True
                if (
                    gnr == 1
                    and self.lasts != self.spindle
                    and self.firmware == "smoothie"
                ):
                    parts.append(f"S{_fmt_num(self.spindle)}")
                    self.lasts = self.spindle
                    needed = True
                if needed:
                    self.lines.append("".join(parts) + cmt)
        else:
            if z is not None:
                self.lines.append(
                    f"G{gnr} X{_fmt_num(x)} Y{_fmt_num(y)} Z{_fmt_num(z)} {feed} {cmt}"
                )
            else:
                self.lines.append(
                    f"G{gnr} X{_fmt_num(x)} Y{_fmt_num(y)} {feed} {cmt}"
                )
        self.lastx = x
        self.lasty = y
        self.lastg = gnr

    # arcs
    def arc(self, gnr, x, y, i, j, cmt, avoid):
        self._move_arc(gnr, x, y, i, j, self.apply_xy_feed, cmt, avoid)

    def _move_arc(self, gnr, x, y, i, j, apply_feed, cmt, avoid):
        feed = ""
        if apply_feed:
            feed = f"F{self._feed_str}"
            self.apply_xy_feed = False
        spd = ""
        if self.firmware == "smoothie":
            spd = f"S{_fmt_num(self.spindle)}"
        if self.no_arcs or avoid:
            self._split_arc(gnr, self.lastx, self.lasty, x, y, i, j, cmt)
        else:
            wrapped = f"({cmt})" if cmt else ""
            self.lines.append(
                f"G{gnr}X{_fmt_num(x)}Y{_fmt_num(y)}"
                f"I{_fmt_num(i)}J{_fmt_num(j)}{feed}{spd}{wrapped}"
            )
            self.lastg = gnr
        self.lastx = x
        self.lasty = y
        self.lastf = self._feed

    @staticmethod
    def _get_angle(x1, y1, x2, y2, i, j):
        radius = math.sqrt(i * i + j * j)
        cos1 = max(-1.0, min(1.0, i / radius))
        a1 = 180.0 - 180.0 * math.acos(cos1) / math.pi
        if j > 0:
            a1 = -a1
        cos2 = max(-1.0, min(1.0, (x1 + i - x2) / radius))
        a2 = 180.0 - 180.0 * math.acos(cos2) / math.pi
        if (y1 + j - y2) > 0:
            a2 = -a2
        da = -(360.0 + a1 - a2)
        if da > 360.0:
            da -= 360.0
        if da < -360.0:
            da += 360.0
        return a1, a2, da

    def _split_arc(self, gnr, x1, y1, x2, y2, i, j, cmt):
        segment_length = 10.0
        radius = math.sqrt(i * i + j * j)
        cx, cy = x1 + i, y1 + j
        a1, a2, da = self._get_angle(x1, y1, x2, y2, i, j)
        da = -(360.0 + a1 - a2)
        if gnr == 3:
            da = abs(360.0 + a2 - a1)
        if da > 360.0:
            da -= 360.0
        if da < -360.0:
            da += 360.0
        if x1 == x2 and y1 == y2:
            da = -360.0 if gnr == 2 else 360.0
        step = math.asin(1.0 / radius) * 180.0 / math.pi
        self.apply_xy_feed = True
        if da > 0:
            angle = a1 + step
            while angle < (a1 + da):
                x = cx + radius * math.cos(math.pi * angle / 180.0)
                y = cy + radius * math.sin(math.pi * angle / 180.0)
                self._move(1, x, y, None, self.apply_xy_feed, cmt)
                angle += step
        else:
            angle = a1 - step
            while angle > (a1 + da):
                x = cx + radius * math.cos(math.pi * angle / 180.0)
                y = cy + radius * math.sin(math.pi * angle / 180.0)
                self._move(1, x, y, None, self.apply_xy_feed, cmt)
                angle -= step
        self._move(1, x2, y2, None, self.apply_xy_feed, "End Arc conversion")


# --- SVG converter (port of GCodeFromSVG.cs) ------------------------------ #


class _SvgConverter:
    def __init__(self, profile):
        self.profile = profile
        self.em = _Gcode(profile)
        self.convert_to_mm = profile.to_mm
        self.smart_bezier = profile.smart_bezier
        self.bezier_accuracy = profile.bezier_accuracy
        self.scale_apply = profile.scale_to_max
        self.max_size = profile.max_size_mm
        self.color_filter = profile.color_filter
        self.reduce = profile.reduce
        self.reduce_value = profile.reduce_value
        self.user_off_x = profile.offset_x
        self.user_off_y = profile.offset_y

        self.scaled_error = 1.0
        self.matrix_group = [_Matrix() for _ in range(10)]
        self.matrix_element = _Matrix()

        self.pen_is_down = True
        self.start_sub_path = True
        self.count_sub_path = 0
        self.start_path = True
        self.start_first_element = True
        self.svg_w_px = 0.0
        self.svg_h_px = 0.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.cur_x = 0.0
        self.cur_y = 0.0
        self.first_x = None
        self.first_y = None
        self.last_x = 0.0
        self.last_y = 0.0
        self.cx_mirror = 0.0
        self.cy_mirror = 0.0

        self.is_reduce_ok = False
        self.last_gcx = 0.0
        self.last_gcy = 0.0
        self.last_set_gcx = 0.0
        self.last_set_gcy = 0.0

    # entry
    def start(self, root):
        self._matrix_for(0)  # ensure size
        self.matrix_element = _Matrix()
        for m in self.matrix_group:
            m.m11, m.m12, m.m21, m.m22, m.ox, m.oy = 1, 0, 0, 1, 0, 0
        self._parse_globals(root)
        self._parse_basic_elements(root, 1)
        self._parse_path(root, 1)
        self._parse_group(root, 1)

    def _matrix_for(self, level):
        while level >= len(self.matrix_group):
            self.matrix_group.append(_Matrix())

    # groups (recursive)
    def _parse_group(self, svg, level):
        for group in _children(svg, "g"):
            self._parse_transform(group, True, level)
            self._parse_basic_elements(group, level)
            self._parse_path(group, level)
            self._parse_group(group, level + 1)

    # globals: dimensions, viewBox, scale, Y-flip
    def _parse_globals(self, svg):
        tmp = _Matrix()
        self.svg_w_px = 0.0
        self.svg_h_px = 0.0
        vb_off_x = vb_off_y = vb_w = vb_h = 0.0

        vb = svg.get("viewBox")
        if vb is not None:
            parts = re.sub(r"\s+", " ", vb).strip().split(" ")
            vb_off_x = -_to_px(parts[0])
            vb_off_y = -_to_px(parts[1])
            vb_w = _to_px(parts[2])
            vb_h = _to_px(parts[3].rstrip(")"))
            tmp.m11 = 1.0
            tmp.m22 = -1.0
            tmp.oy = vb_h

        scale = (1.0 / _MM2PX) if self.convert_to_mm else (1.0 / _IN2PX)

        w_attr = svg.get("width")
        if w_attr is not None:
            self.svg_w_px = _to_px(w_attr)
            tmp.m11 = scale
            if vb_w > 0:
                self.scaled_error = scale * self.svg_w_px / vb_w
                tmp.m11 = self.scaled_error
                tmp.ox = (vb_off_x * self.svg_w_px / vb_w) * scale

        h_attr = svg.get("height")
        if h_attr is not None:
            self.svg_h_px = _to_px(h_attr)
            tmp.m22 = -scale
            tmp.oy = scale * self.svg_h_px
            if vb_h > 0:
                tmp.m22 = -scale * self.svg_h_px / vb_h
                tmp.oy = (-vb_off_y * self.svg_h_px / vb_h + self.svg_h_px) * scale

        new_w = max(self.svg_w_px, vb_w)
        new_h = max(self.svg_h_px, vb_h)
        if new_w > 0 and new_h > 0 and self.scale_apply:
            gscale = self.max_size / max(new_w, new_h)
            tmp.scale(gscale, gscale)
            if self.convert_to_mm:
                tmp.scale(_MM2PX, _MM2PX)
            else:
                tmp.scale(_IN2PX, _IN2PX)

        tmp.ox += self.user_off_x
        tmp.oy += self.user_off_y

        for k in range(len(self.matrix_group)):
            self.matrix_group[k] = tmp.copy()
        self.matrix_element = tmp.copy()

    # transforms
    @staticmethod
    def _get_text_between(source, s1):
        start = source.find(s1) + len(s1)
        for i in range(start, len(source)):
            c = source[i]
            if not (c.isdigit() or c in ".,- e"):
                return source[start:i]
        return source[start:len(source) - 1]

    @classmethod
    def _parse_transform_str(cls, transform):
        tmp = _Matrix()
        if "translate" in transform:
            coord = cls._get_text_between(transform, "translate(")
            split = coord.split(",") if "," in coord else coord.split(" ")
            tmp.ox = _to_px(split[0])
            if len(split) > 1:
                tmp.oy = _to_px(split[1].rstrip(")"))
        if "scale" in transform:
            coord = cls._get_text_between(transform, "scale(")
            split = coord.split(",") if "," in coord else coord.split(" ")
            tmp.m11 = _to_px(split[0])
            if len(split) > 1:
                tmp.m22 = _to_px(split[1])
            else:
                tmp.m11 = _to_px(coord)
                tmp.m22 = _to_px(coord)
        if "rotate" in transform:
            coord = cls._get_text_between(transform, "rotate(")
            split = coord.split(",") if "," in coord else coord.split(" ")
            angle = _to_px(split[0])
            px = _to_px(split[1]) if len(split) == 3 else 0.0
            py = _to_px(split[2]) if len(split) == 3 else 0.0
            tmp.rotate_at(angle, px, py)
        if "matrix" in transform:
            coord = cls._get_text_between(transform, "matrix(")
            split = coord.split(",") if "," in coord else coord.split(" ")
            tmp.m11 = _to_px(split[0])
            tmp.m12 = _to_px(split[1])
            tmp.m21 = _to_px(split[2])
            tmp.m22 = _to_px(split[3])
            tmp.ox = _to_px(split[4])
            tmp.oy = _to_px(split[5])
        return tmp

    def _parse_transform(self, element, is_group, level):
        self._matrix_for(level)
        final = _Matrix()
        t = element.get("transform")
        if t:
            words = [w for w in re.split(r"trans|sca|rot|mat", t) if w]
            for word in words:
                if word.startswith("late"):
                    final = _Matrix.multiply(
                        self._parse_transform_str("trans" + word), final
                    )
                elif word.startswith("le"):
                    final = _Matrix.multiply(
                        self._parse_transform_str("sca" + word), final
                    )
                elif word.startswith("ate"):
                    final = _Matrix.multiply(
                        self._parse_transform_str("rot" + word), final
                    )
                elif word.startswith("rix"):
                    final = _Matrix.multiply(
                        self._parse_transform_str("mat" + word), final
                    )
        if is_group:
            self.matrix_group[level] = _Matrix()
            if level > 0:
                for k in range(level, len(self.matrix_group)):
                    self.matrix_group[k] = _Matrix.multiply(
                        final, self.matrix_group[level - 1]
                    )
            else:
                self.matrix_group[0] = final
            self.matrix_element = self.matrix_group[level]
        else:
            self.matrix_element = _Matrix.multiply(final, self.matrix_group[level])
        return final

    # color filter
    @staticmethod
    def _parse_style(elem):
        attr = elem.get("style")
        if not attr:
            return {}
        out = {}
        for piece in attr.replace(" ", "").split(";"):
            if not piece:
                continue
            kv = piece.split(":")
            if len(kv) == 2 and kv[1] != "none":
                out[kv[0]] = kv[1]
        return out

    @staticmethod
    def _parse_color_attrs(elem):
        out = {}
        for key in ("stroke", "fill"):
            v = elem.get(key)
            if v is not None and v != "none":
                out[key] = v
        return out

    def _filter_by_color(self, elem):
        f = self.color_filter
        if f == "all":
            return True
        style = self._parse_style(elem)
        attrs = self._parse_color_attrs(elem)
        color = None
        for src, key in ((style, "stroke"), (style, "fill"),
                         (attrs, "stroke"), (attrs, "fill")):
            if key in src:
                color = src[key]
                break
        if color is None:
            return True
        rgb = _parse_color(color)
        if rgb is None:
            return True
        r, g, b = rgb
        high, low = 127, 20
        if f == "red":
            return r >= high and g <= low and b <= low
        if f == "green":
            return r <= low and g > high and b <= low
        if f == "blue":
            return r <= low and g <= low and b > high
        if f == "black":
            return r <= low and g <= low and b <= low
        return True

    # basic shapes
    def _parse_basic_elements(self, svg, level):
        forms = ("rect", "circle", "ellipse", "line", "polyline", "polygon",
                 "text", "image")
        for form in forms:
            for elem in _children(svg, form):
                if not self._filter_by_color(elem):
                    continue
                if self.start_first_element:
                    self._pen_up("1st shape")
                    self.start_first_element = False
                self.offset_x = 0.0
                self.offset_y = 0.0
                old_matrix = self.matrix_element
                avoid = False
                self._parse_transform(elem, False, level)

                def px(name, ext=1.0):
                    v = elem.get(name)
                    return _to_px(v, ext) if v is not None else 0.0

                x = px("x")
                y = px("y")
                x1 = px("x1")
                y1 = px("y1")
                x2 = px("x2")
                y2 = px("y2")
                width = px("width", self.svg_w_px)
                height = px("height", self.svg_h_px)
                rx = px("rx")
                ry = px("ry")
                cx = px("cx")
                cy = px("cy")
                r = px("r")
                points = (elem.get("points") or "").split(" ")

                if form == "rect":
                    if ry == 0:
                        ry = rx
                    elif rx == 0:
                        rx = ry
                    elif rx != ry:
                        rx = min(rx, ry)
                        ry = rx
                    x += self.offset_x
                    y += self.offset_y
                    self._start_path(x + rx, y + height, form)
                    self._move_to(x + width - rx, y + height, form + " a1")
                    if rx > 0:
                        self._arc_ccw(x + width, y + height - ry, 0, -ry, form, avoid)
                    self._move_to(x + width, y + ry, form + " b1")
                    if rx > 0:
                        self._arc_ccw(x + width - rx, y, -rx, 0, form, avoid)
                    self._move_to(x + rx, y, form + " a2")
                    if rx > 0:
                        self._arc_ccw(x, y + ry, 0, ry, form, avoid)
                    self._move_to(x, y + height - ry, form + " b2")
                    if rx > 0:
                        self._arc_ccw(x + rx, y + height, rx, 0, form, avoid)
                        self._move_to(x + rx, y + height, form)
                    self._stop_path(form)
                elif form == "circle":
                    cx += self.offset_x
                    cy += self.offset_y
                    self._start_path(cx + r, cy, form)
                    self._arc_ccw(cx + r, cy, -r, 0, form, avoid)
                    self._stop_path(form)
                elif form == "ellipse":
                    cx += self.offset_x
                    cy += self.offset_y
                    self._start_path(cx + rx, cy, form)
                    self.is_reduce_ok = True
                    self._calc_arc(cx + rx, cy, rx, ry, 0, 1, 1, cx - rx, cy)
                    self._calc_arc(cx - rx, cy, rx, ry, 0, 1, 1, cx + rx, cy)
                    self._stop_path(form)
                elif form == "line":
                    x1 += self.offset_x
                    y1 += self.offset_y
                    self._start_path(x1, y1, form)
                    self._move_to(x2, y2, form)
                    self._stop_path(form)
                elif form in ("polyline", "polygon"):
                    self.offset_x = 0.0
                    self.offset_y = 0.0
                    index = 0
                    while index < len(points) and not points[index]:
                        index += 1
                    if index < len(points) and "," in points[index]:
                        coord = points[index].split(",")
                        x = _to_px(coord[0])
                        y = _to_px(coord[1])
                        x1, y1 = x, y
                        self._start_path(x, y, form)
                        self.is_reduce_ok = True
                        for k in range(index + 1, len(points)):
                            if len(points[k]) > 3:
                                coord = points[k].split(",")
                                x = _to_px(coord[0]) + self.offset_x
                                y = _to_px(coord[1]) + self.offset_y
                                self._move_to(x, y, form)
                        if form == "polygon":
                            self._move_to(x1, y1, form)
                        self._stop_path(form)
                # text / image: unsupported, silently skipped (faithful)
                self.matrix_element = old_matrix

    # paths
    def _parse_path(self, svg, level):
        for elem in _children(svg, "path"):
            if not self._filter_by_color(elem):
                continue
            self.offset_x = 0.0
            self.offset_y = 0.0
            self.cur_x = self.offset_x
            self.cur_y = self.offset_x  # faithful to source
            self.first_x = None
            self.first_y = None
            self.start_path = True
            self.start_sub_path = True
            self.last_x = self.offset_x
            self.last_y = self.offset_y
            d = elem.get("d")
            old_matrix = self.matrix_element
            self._parse_transform(elem, False, level)
            if d:
                tokens = [t for t in _DSPLIT.split(d) if t and t.strip()]
                for token in tokens:
                    self._parse_path_command(token)
            self._pen_up("End path")
            self.matrix_element = old_matrix

    def _parse_path_command(self, svg_path):
        command = svg_path[0]
        cmd = command.upper()
        absolute = (cmd == command)
        remaining = svg_path[1:]
        args = [_to_px(m.group(0)) for m in _ARG.finditer(remaining)]

        if cmd == "M":
            for i in range(0, len(args) - 1, 2):
                if absolute or self.start_path:
                    self.cur_x = args[i] + self.offset_x
                    self.cur_y = args[i + 1] + self.offset_y
                else:
                    self.cur_x = args[i] + self.last_x
                    self.cur_y = args[i + 1] + self.last_y
                if self.start_sub_path:
                    if self.count_sub_path > 0:
                        self._stop_path("Stop Path")
                    self.count_sub_path += 1
                    self._start_path(self.cur_x, self.cur_y, command)
                    self.is_reduce_ok = True
                    self.first_x = self.cur_x
                    self.first_y = self.cur_y
                    self.start_path = False
                    self.start_sub_path = False
                else:
                    if i <= 1:
                        self._start_path(self.cur_x, self.cur_y, command)
                    else:
                        self._move_to(self.cur_x, self.cur_y, command)
                if self.first_x is None:
                    self.first_x = self.cur_x
                if self.first_y is None:
                    self.first_y = self.cur_y
                self.last_x = self.cur_x
                self.last_y = self.cur_y
            self.cx_mirror = self.cur_x
            self.cy_mirror = self.cur_y

        elif cmd == "Z":
            if self.first_x is None:
                self.first_x = self.cur_x
            if self.first_y is None:
                self.first_y = self.cur_y
            self._move_to(self.first_x, self.first_y, command)
            self.last_x = self.first_x
            self.last_y = self.first_y
            self.first_x = None
            self.first_y = None
            self.start_sub_path = True
            self._stop_path("Z")

        elif cmd == "L":
            for i in range(0, len(args) - 1, 2):
                if absolute:
                    self.cur_x = args[i] + self.offset_x
                    self.cur_y = args[i + 1] + self.offset_y
                else:
                    self.cur_x = self.last_x + args[i]
                    self.cur_y = self.last_y + args[i + 1]
                self._move_to(self.cur_x, self.cur_y, command)
                self.last_x = self.cur_x
                self.last_y = self.cur_y
                self.cx_mirror = self.cur_x
                self.cy_mirror = self.cur_y
            self.start_sub_path = True

        elif cmd == "H":
            for i in range(len(args)):
                if absolute:
                    self.cur_x = args[i] + self.offset_x
                else:
                    self.cur_x = self.last_x + args[i]
                self.cur_y = self.last_y
                self._move_to(self.cur_x, self.cur_y, command)
                self.last_x = self.cur_x
                self.last_y = self.cur_y
                self.cx_mirror = self.cur_x
                self.cy_mirror = self.cur_y
            self.start_sub_path = True

        elif cmd == "V":
            for i in range(len(args)):
                self.cur_x = self.last_x
                if absolute:
                    self.cur_y = args[i] + self.offset_y
                else:
                    self.cur_y = self.last_y + args[i]
                self._move_to(self.cur_x, self.cur_y, command)
                self.last_x = self.cur_x
                self.last_y = self.cur_y
                self.cx_mirror = self.cur_x
                self.cy_mirror = self.cur_y
            self.start_sub_path = True

        elif cmd == "A":
            for rep in range(0, len(args) - 6, 7):
                rx = args[rep]
                ry = args[rep + 1]
                rot = args[rep + 2]
                large = args[rep + 3]
                sweep = args[rep + 4]
                if absolute:
                    nx = args[rep + 5] + self.offset_x
                    ny = args[rep + 6] + self.offset_y
                else:
                    nx = args[rep + 5] + self.last_x
                    ny = args[rep + 6] + self.last_y
                self._calc_arc(self.last_x, self.last_y, rx, ry, rot, large, sweep, nx, ny)
                self.last_x = nx
                self.last_y = ny
            self.start_sub_path = True

        elif cmd == "C":
            for rep in range(0, len(args), 6):
                if rep + 5 < len(args):
                    if absolute:
                        cx1 = args[rep] + self.offset_x
                        cy1 = args[rep + 1] + self.offset_y
                        cx2 = args[rep + 2] + self.offset_x
                        cy2 = args[rep + 3] + self.offset_y
                        cx3 = args[rep + 4] + self.offset_x
                        cy3 = args[rep + 5] + self.offset_y
                    else:
                        cx1 = self.last_x + args[rep]
                        cy1 = self.last_y + args[rep + 1]
                        cx2 = self.last_x + args[rep + 2]
                        cy2 = self.last_y + args[rep + 3]
                        cx3 = self.last_x + args[rep + 4]
                        cy3 = self.last_y + args[rep + 5]
                    pts = [(self.last_x, self.last_y), (cx1, cy1), (cx2, cy2), (cx3, cy3)]
                    b = self._bezier(pts)
                    for k in range(1, len(b)):
                        self._move_to(b[k][0], b[k][1], command)
                    self.cx_mirror = cx3 - (cx2 - cx3)
                    self.cy_mirror = cy3 - (cy2 - cy3)
                    self.last_x = cx3
                    self.last_y = cy3
            self.start_sub_path = True

        elif cmd == "S":
            for rep in range(0, len(args) - 3, 4):
                if absolute:
                    cx2 = args[rep] + self.offset_x
                    cy2 = args[rep + 1] + self.offset_y
                    cx3 = args[rep + 2] + self.offset_x
                    cy3 = args[rep + 3] + self.offset_y
                else:
                    cx2 = self.last_x + args[rep]
                    cy2 = self.last_y + args[rep + 1]
                    cx3 = self.last_x + args[rep + 2]
                    cy3 = self.last_y + args[rep + 3]
                pts = [(self.last_x, self.last_y), (self.cx_mirror, self.cy_mirror),
                       (cx2, cy2), (cx3, cy3)]
                b = self._bezier(pts)
                for k in range(1, len(b)):
                    self._move_to(b[k][0], b[k][1], command)
                self.cx_mirror = cx3 - (cx2 - cx3)
                self.cy_mirror = cy3 - (cy2 - cy3)
                self.last_x = cx3
                self.last_y = cy3
            self.start_sub_path = True

        elif cmd == "Q":
            for rep in range(0, len(args) - 3, 4):
                if absolute:
                    cx2 = args[rep] + self.offset_x
                    cy2 = args[rep + 1] + self.offset_y
                    cx3 = args[rep + 2] + self.offset_x
                    cy3 = args[rep + 3] + self.offset_y
                else:
                    cx2 = self.last_x + args[rep]
                    cy2 = self.last_y + args[rep + 1]
                    cx3 = self.last_x + args[rep + 2]
                    cy3 = self.last_y + args[rep + 3]
                qpx1 = (cx2 - self.last_x) * 2 / 3 + self.last_x
                qpy1 = (cy2 - self.last_y) * 2 / 3 + self.last_y
                qpx2 = (cx2 - cx3) * 2 / 3 + cx3
                qpy2 = (cy2 - cy3) * 2 / 3 + cy3
                pts = [(self.last_x, self.last_y), (qpx1, qpy1), (qpx2, qpy2), (cx3, cy3)]
                self.cx_mirror = cx3 - (cx2 - cx3)
                self.cy_mirror = cy3 - (cy2 - cy3)
                self.last_x = cx3
                self.last_y = cy3
                b = self._bezier(pts)
                for k in range(1, len(b)):
                    self._move_to(b[k][0], b[k][1], command)
            self.start_sub_path = True

        elif cmd == "T":
            for rep in range(0, len(args) - 1, 2):
                if absolute:
                    cx3 = args[rep] + self.offset_x
                    cy3 = args[rep + 1] + self.offset_y
                else:
                    cx3 = self.last_x + args[rep]
                    cy3 = self.last_y + args[rep + 1]
                qpx1 = (self.cx_mirror - self.last_x) * 2 / 3 + self.last_x
                qpy1 = (self.cy_mirror - self.last_y) * 2 / 3 + self.last_y
                qpx2 = (self.cx_mirror - cx3) * 2 / 3 + cx3
                qpy2 = (self.cy_mirror - cy3) * 2 / 3 + cy3
                pts = [(self.last_x, self.last_y), (qpx1, qpy1), (qpx2, qpy2), (cx3, cy3)]
                self.cx_mirror = cx3
                self.cy_mirror = cy3
                self.last_x = cx3
                self.last_y = cy3
                b = self._bezier(pts)
                for k in range(1, len(b)):
                    self._move_to(b[k][0], b[k][1], command)
            self.start_sub_path = True

    # elliptical arc -> cubic beziers (SvgArcSegment algorithm)
    def _calc_arc(self, sx, sy, rx, ry, angle, size, sweep, ex, ey):
        if rx == 0.0 and ry == 0.0:
            return
        sin_phi = math.sin(angle * math.pi / 180.0)
        cos_phi = math.cos(angle * math.pi / 180.0)
        x1d = cos_phi * (sx - ex) / 2.0 + sin_phi * (sy - ey) / 2.0
        y1d = -sin_phi * (sx - ex) / 2.0 + cos_phi * (sy - ey) / 2.0
        numerator = (rx * rx * ry * ry - rx * rx * y1d * y1d - ry * ry * x1d * x1d)
        if numerator < 0.0:
            s = math.sqrt(1.0 - numerator / (rx * rx * ry * ry))
            rx *= s
            ry *= s
            root = 0.0
        else:
            sign = -1.0 if ((size == 1 and sweep == 1) or (size == 0 and sweep == 0)) else 1.0
            root = sign * math.sqrt(
                numerator / (rx * rx * y1d * y1d + ry * ry * x1d * x1d)
            )
        cxd = root * rx * y1d / ry
        cyd = -root * ry * x1d / rx
        cx = cos_phi * cxd - sin_phi * cyd + (sx + ex) / 2.0
        cy = sin_phi * cxd + cos_phi * cyd + (sy + ey) / 2.0
        theta1 = self._vector_angle(1.0, 0.0, (x1d - cxd) / rx, (y1d - cyd) / ry)
        dtheta = self._vector_angle(
            (x1d - cxd) / rx, (y1d - cyd) / ry,
            (-x1d - cxd) / rx, (-y1d - cyd) / ry,
        )
        if sweep == 0 and dtheta > 0:
            dtheta -= 2.0 * math.pi
        elif sweep == 1 and dtheta < 0:
            dtheta += 2.0 * math.pi
        segments = int(math.ceil(abs(dtheta / (math.pi / 2.0))))
        if segments == 0:
            return
        delta = dtheta / segments
        t = 8.0 / 3.0 * math.sin(delta / 4.0) * math.sin(delta / 4.0) / math.sin(delta / 2.0)
        start_x = sx
        start_y = sy
        for _ in range(segments):
            cos1 = math.cos(theta1)
            sin1 = math.sin(theta1)
            theta2 = theta1 + delta
            cos2 = math.cos(theta2)
            sin2 = math.sin(theta2)
            ep_x = cos_phi * rx * cos2 - sin_phi * ry * sin2 + cx
            ep_y = sin_phi * rx * cos2 + cos_phi * ry * sin2 + cy
            dx1 = t * (-cos_phi * rx * sin1 - sin_phi * ry * cos1)
            dy1 = t * (-sin_phi * rx * sin1 + cos_phi * ry * cos1)
            dxe = t * (cos_phi * rx * sin2 + sin_phi * ry * cos2)
            dye = t * (sin_phi * rx * sin2 - cos_phi * ry * cos2)
            pts = [(start_x, start_y), (start_x + dx1, start_y + dy1),
                   (ep_x + dxe, ep_y + dye), (ep_x, ep_y)]
            b = self._bezier(pts)
            for k in range(1, len(b)):
                self._move_to(b[k][0], b[k][1], "arc")
            theta1 = theta2
            start_x = ep_x
            start_y = ep_y

    @staticmethod
    def _vector_angle(ux, uy, vx, vy):
        ta = math.atan2(uy, ux)
        tb = math.atan2(vy, vx)
        if tb >= ta:
            return tb - ta
        return math.pi * 2 - (ta - tb)

    def _bezier(self, cp4):
        if not self.smart_bezier:
            return _bezier_old(cp4, self.bezier_accuracy)
        return _flatten_to(cp4, 0.2 / self.scaled_error)

    # coordinate transforms
    def _translate_xy(self, x, y):
        return self.matrix_element.transform(x, y)

    def _translate_ij(self, i, j):
        m = self.matrix_element
        return (i * m.m11 + j * m.m21, i * m.m12 + j * m.m22)

    # G-code helpers (penIsDown tracking + transform application)
    def _start_path(self, x, y, cmt):
        cx, cy = self._translate_xy(x, y)
        self.last_gcx, self.last_gcy = cx, cy
        self.last_set_gcx, self.last_set_gcy = cx, cy
        self._pen_up(cmt)
        self.em.move_rapid(cx, cy, cmt)
        self.pen_is_down = False
        self.is_reduce_ok = False

    def _stop_path(self, cmt):
        if self.reduce:
            if self.last_set_gcx != self.last_gcx or self.last_set_gcy != self.last_gcy:
                self.em.move_to(self.last_gcx, self.last_gcy, "restore Point")
        self._pen_up(cmt)

    def _move_to(self, x, y, cmt):
        cx, cy = self._translate_xy(x, y)
        self._pen_down(cmt)
        reject = False
        if self.reduce and self.is_reduce_ok:
            dist = math.hypot(cx - self.last_set_gcx, cy - self.last_set_gcy)
            if dist < self.reduce_value:
                reject = True
            else:
                self.last_set_gcx, self.last_set_gcy = cx, cy
        if not self.reduce or not reject:
            self.em.move_to(cx, cy, cmt)
        self.last_gcx, self.last_gcy = cx, cy

    def _arc_ccw(self, x, y, i, j, cmt, avoid=False):
        cx, cy = self._translate_xy(x, y)
        ci, cj = self._translate_ij(i, j)
        self._pen_down(cmt)
        if self.reduce and self.is_reduce_ok:
            if self.last_set_gcx != self.last_gcx or self.last_set_gcy != self.last_gcy:
                self.em.move_to(self.last_gcx, self.last_gcy, cmt)
        self.em.arc(3, cx, cy, ci, cj, cmt, avoid)

    def _pen_up(self, cmt):
        if self.pen_is_down:
            self.em.pen_up(cmt)
        self.pen_is_down = False

    def _pen_down(self, cmt):
        if not self.pen_is_down:
            self.em.pen_down(cmt)
        self.pen_is_down = True


def convert(svg_path, profile):
    """Parse the SVG file and return the body G-code as a list of lines
    (no header/footer, no trailing newlines)."""
    from pathlib import Path

    p = Path(svg_path)
    if not p.exists():
        raise FileNotFoundError(svg_path)
    root = ET.fromstring(p.read_bytes())
    conv = _SvgConverter(profile)
    conv.start(root)
    return conv.em.lines
