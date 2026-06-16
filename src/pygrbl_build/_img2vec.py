"""Image -> vector G-code engine, ported from LaserGRBL's "Vectorize!".

This is a faithful Python port of LaserGRBL's raster-to-vector pipeline:
the Potrace tracer (`CsPotrace.cs`, itself Wolfgang Nagl's C# port of Peter
Selinger's Potrace), the cubic-Bezier-to-biarc approximation
(`BezierToBiarc/*`) and the G-code emitter (`CsPotraceExportGCODE.cs`),
plus the image preprocessing that feeds the tracer (`ImageProcessor.cs` +
`ImageTransform.cs`, the Vectorize path only). Pure Python; the only
dependency is Pillow, used to load, resize and threshold the image.

The flow, end to end:

    image -> [resize + grayscale + whitenize + threshold] -> flip-Y
          -> Potrace (bitmap -> closed contours of lines + cubic Beziers)
          -> each Bezier -> biarcs -> G2/G3 arcs (or G1 fallback) -> G-code

Public surface lives in ``pygrbl_build.__init__`` (``Img2VectorProfile``
and ``img2vector_gcode``); this module is the engine and is private.

Fidelity note: LaserGRBL's biarc geometry runs in 32-bit ``float`` and its
image resize is GDI+ bicubic; this port uses Python ``float`` (double) and
Pillow's bicubic. The algorithm and parameters match exactly, so the
output is the same trace, but it is not guaranteed byte-for-byte identical
(the user asked for practical parity, not bit-exact parity). Wherever the
original arithmetic is delicate (C# integer division truncates toward
zero; its hand-rolled ``mod``; the known ``Arc.LinearLength`` bug) the
behaviour is replicated on purpose and called out in comments.

Coordinate model: the bitmap is flipped vertically before tracing (exactly
like LaserGRBL), so Y already grows upward; the emitter only divides pixel
coordinates by the resolution to get millimetres and adds the offset.
"""

import math


# --------------------------------------------------------------------------- #
# Numeric helpers
# --------------------------------------------------------------------------- #


def _fmt_num(v):
    """Format a coordinate like LaserGRBL's "0.###": up to 3 decimals,
    trailing zeros stripped, whole numbers with no decimal point. Python's
    ``%.3f`` rounds half-to-even, same as .NET's ToString("0.###")."""
    s = f"{v:.3f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def _cdiv(a, b):
    """Integer division that truncates toward zero, like C#'s ``/`` on
    ints. Python's ``//`` floors (toward -inf), which differs for a
    negative quotient — Potrace relies on the C# semantics (findPath's
    pixel offsets, calcLon's ``a/-b`` and ``-c/d``)."""
    q = a // b
    if (a % b != 0) and ((a < 0) != (b < 0)):
        q += 1
    return q


def _mod(a, n):
    """Potrace's hand-rolled modulo (CsPotrace.cs ``mod``). Kept verbatim
    rather than using Python's ``%`` to guarantee identical wrap-around."""
    return a % n if a >= n else (a if a >= 0 else n - 1 - (-1 - a) % n)


def _sign(i):
    return 1 if i > 0 else (-1 if i < 0 else 0)


# --------------------------------------------------------------------------- #
# SECTION A — Potrace core (port of CsPotrace.cs)
# --------------------------------------------------------------------------- #

_POTRACE_CORNER = 1
_POTRACE_CURVETO = 2

# Curve kinds (CsPotrace.CurveKind)
_LINE = 0
_BEZIER = 1


class _IPoint:
    """Integer lattice point (CsPotrace ``Point`` struct). Mutable: calcLon
    mutates ``cur``/``off``/``dk``/``constraint`` in place, like the C#
    struct fields."""

    __slots__ = ("x", "y")

    def __init__(self, x=0, y=0):
        self.x = x
        self.y = y


class _DPoint:
    """Double-precision point (CsPotrace ``dPoint`` class). Reference type
    in C#; several call sites use ``.copy()`` explicitly, replicated here."""

    __slots__ = ("X", "Y")

    def __init__(self, X=0.0, Y=0.0):
        self.X = X
        self.Y = Y

    def copy(self):
        return _DPoint(self.X, self.Y)


class _Curve:
    """Output segment (CsPotrace ``Curve`` struct): a line or a cubic
    Bezier. For a Bezier, (A, ControlPointA, ControlPointB, B) =
    (start, c[j*3], c[j*3+1], c[j*3+2])."""

    __slots__ = ("kind", "A", "ControlPointA", "ControlPointB", "B")

    def __init__(self, kind, A, ControlPointA, ControlPointB, B):
        self.kind = kind
        self.A = A
        self.ControlPointA = ControlPointA
        self.ControlPointB = ControlPointB
        self.B = B

    @property
    def linear_length(self):
        dX = self.B.X - self.A.X
        dY = self.B.Y - self.A.Y
        return math.sqrt(dX * dX + dY * dY)


class _Sum:
    __slots__ = ("x", "y", "xy", "x2", "y2")

    def __init__(self, x, y, xy, x2, y2):
        self.x = x
        self.y = y
        self.xy = xy
        self.x2 = x2
        self.y2 = y2


class _Bitmap:
    """Binary bitmap (CsPotrace ``Bitmap_p``): row-major ``data`` with
    index ``w*y + x``, 1 = foreground/black."""

    __slots__ = ("w", "h", "data")

    def __init__(self, w, h, data=None):
        self.w = w
        self.h = h
        self.data = bytearray(w * h) if data is None else data

    @property
    def size(self):
        return self.w * self.h

    def at(self, x, y):
        return (0 <= x < self.w and 0 <= y < self.h
                and self.data[self.w * y + x] == 1)

    def index(self, i):
        y = i // self.w
        return _IPoint(i - y * self.w, y)

    def flip(self, x, y):
        idx = self.w * y + x
        self.data[idx] = 0 if self.data[idx] == 1 else 1

    def copy(self):
        return _Bitmap(self.w, self.h, bytearray(self.data))


class _Path:
    __slots__ = ("m", "area", "len", "sign", "pt", "minX", "minY", "maxX",
                 "maxY", "x0", "y0", "po", "lon", "sums", "curve")

    def __init__(self):
        self.m = 0
        self.area = 0
        self.len = 0
        self.sign = "?"
        self.pt = []
        self.minX = 100000
        self.minY = 100000
        self.maxX = -1
        self.maxY = -1
        self.x0 = 0.0
        self.y0 = 0.0
        self.po = None
        self.lon = None
        self.sums = []
        self.curve = None


class _Privcurve:
    __slots__ = ("n", "tag", "vertex", "c", "alpha", "alpha0", "beta",
                 "alphacurve")

    def __init__(self, count):
        self.n = count
        self.tag = [0] * count
        self.vertex = [None] * count
        self.alpha = [0.0] * count
        self.alpha0 = [0.0] * count
        self.beta = [0.0] * count
        self.c = [None] * (count * 3)
        self.alphacurve = 0


class _Quad:
    __slots__ = ("data",)

    def __init__(self):
        self.data = [0.0] * 9

    def at(self, x, y):
        return self.data[x * 3 + y]


class _Opti:
    __slots__ = ("pen", "c", "t", "s", "alpha")

    def __init__(self):
        self.pen = 0.0
        self.c = [None, None]
        self.t = 0.0
        self.s = 0.0
        self.alpha = 0.0


# --- Potrace auxiliary geometry (module functions; mirror CsPotrace) ------- #


def _interval(lam, a, b):
    return _DPoint(a.X + lam * (b.X - a.X), a.Y + lam * (b.Y - a.Y))


def _dorth_infty(p0, p2):
    """90 degrees CCW from p2-p0, restricted to a major wind direction."""
    return _DPoint(-_sign(p2.Y - p0.Y), _sign(p2.X - p0.X))


def _ddenom(p0, p2):
    r = _dorth_infty(p0, p2)
    return r.Y * (p2.X - p0.X) - r.X * (p2.Y - p0.Y)


def _dpara(p0, p1, p2):
    """(p1-p0) x (p2-p0), area of the parallelogram."""
    x1 = p1.X - p0.X
    y1 = p1.Y - p0.Y
    x2 = p2.X - p0.X
    y2 = p2.Y - p0.Y
    return x1 * y2 - x2 * y1


def _cprod(p0, p1, p2, p3):
    """(p1-p0) x (p3-p2)."""
    x1 = p1.X - p0.X
    y1 = p1.Y - p0.Y
    x2 = p3.X - p2.X
    y2 = p3.Y - p2.Y
    return x1 * y2 - x2 * y1


def _iprod(p0, p1, p2):
    """(p1-p0) . (p2-p0)."""
    x1 = p1.X - p0.X
    y1 = p1.Y - p0.Y
    x2 = p2.X - p0.X
    y2 = p2.Y - p0.Y
    return x1 * x2 + y1 * y2


def _iprod1(p0, p1, p2, p3):
    """(p1-p0) . (p3-p2)."""
    x1 = p1.X - p0.X
    y1 = p1.Y - p0.Y
    x2 = p3.X - p2.X
    y2 = p3.Y - p2.Y
    return x1 * x2 + y1 * y2


def _ddist(p, q):
    return math.sqrt((p.X - q.X) * (p.X - q.X) + (p.Y - q.Y) * (p.Y - q.Y))


def _xprodi(p1, p2):
    """Integer cross product of two _IPoints."""
    return p1.x * p2.y - p1.y * p2.x


def _cyclic(a, b, c):
    """1 if a <= b < c cyclically (mod n)."""
    if a <= c:
        return a <= b and b < c
    return a <= b or b < c


def _quadform(Q, w):
    v = (w.X, w.Y, 1.0)
    s = 0.0
    for i in range(3):
        for j in range(3):
            s += v[i] * Q.at(i, j) * v[j]
    return s


def _bezier3(t, p0, p1, p2, p3):
    """Point of the cubic Bezier (p0,p1,p2,p3) at t (Potrace's ``bezier``)."""
    s = 1 - t
    return _DPoint(
        s * s * s * p0.X + 3 * (s * s * t) * p1.X + 3 * (t * t * s) * p2.X
        + t * t * t * p3.X,
        s * s * s * p0.Y + 3 * (s * s * t) * p1.Y + 3 * (t * t * s) * p2.Y
        + t * t * t * p3.Y,
    )


def _tangent(p0, p1, p2, p3, q0, q1):
    """t in [0,1] on the Bezier (p0..p3) tangent to q1-q0, or -1.0."""
    A = _cprod(p0, p1, q0, q1)
    B = _cprod(p1, p2, q0, q1)
    C = _cprod(p2, p3, q0, q1)

    a = A - 2 * B + C
    b = -2 * A + 2 * B
    c = A

    d = b * b - 4 * a * c
    if a == 0 or d < 0:
        return -1.0
    s = math.sqrt(d)
    r1 = (-b + s) / (2 * a)
    r2 = (-b - s) / (2 * a)
    if 0 <= r1 <= 1:
        return r1
    elif 0 <= r2 <= 1:
        return r2
    return -1.0


def _pointslope(path, i, j, ctr, dir):
    """Center + slope of line i..j (assume i<j). Mutates ctr, dir."""
    n = path.len
    sums = path.sums
    r = 0
    while j >= n:
        j -= n
        r += 1
    while i >= n:
        i -= n
        r -= 1
    while j < 0:
        j += n
        r -= 1
    while i < 0:
        i += n
        r += 1

    x = sums[j + 1].x - sums[i].x + r * sums[n].x
    y = sums[j + 1].y - sums[i].y + r * sums[n].y
    x2 = sums[j + 1].x2 - sums[i].x2 + r * sums[n].x2
    xy = sums[j + 1].xy - sums[i].xy + r * sums[n].xy
    y2 = sums[j + 1].y2 - sums[i].y2 + r * sums[n].y2
    k = j + 1 - i + r * n

    ctr.X = x / k
    ctr.Y = y / k

    a = (x2 - x * x / k) / k
    b = (xy - x * y / k) / k
    c = (y2 - y * y / k) / k

    lambda2 = (a + c + math.sqrt((a - c) * (a - c) + 4 * b * b)) / 2

    a -= lambda2
    c -= lambda2

    l = 0.0
    if abs(a) >= abs(c):
        l = math.sqrt(a * a + b * b)
        if l != 0:
            dir.X = -b / l
            dir.Y = a / l
    else:
        l = math.sqrt(c * c + b * b)
        if l != 0:
            dir.X = -c / l
            dir.Y = b / l
    if l == 0:
        # k=4 degenerate: eigenvalues coincide.
        dir.X = dir.Y = 0.0


class _Potrace:
    """Stateful tracer mirroring CsPotrace's static ``Potrace`` class. One
    instance per trace keeps ``bm`` and ``pathlist`` local (the C# uses
    static fields; an instance is the clean Python equivalent)."""

    def __init__(self, turnpolicy="minority", turdsize=2, alphamax=1.0,
                 opttolerance=0.2, curveoptimizing=True):
        self.turnpolicy = turnpolicy
        self.turdsize = turdsize
        self.alphamax = alphamax
        self.opttolerance = opttolerance
        self.curveoptimizing = curveoptimizing
        self.bm = None
        self.pathlist = []

    # --- decomposition ---------------------------------------------------- #

    def _find_next(self, bm1, point):
        i = bm1.w * point.y + point.x
        size = bm1.size
        data = bm1.data
        while i < size and data[i] != 1:
            i += 1
        if i >= size:
            return False, point
        return True, bm1.index(i)

    def _majority(self, bm1, x, y):
        for i in range(2, 5):
            ct = 0
            for a in range(-i + 1, i):
                ct += 1 if bm1.at(x + a, y + i - 1) else -1
                ct += 1 if bm1.at(x + i - 1, y + a - 1) else -1
                ct += 1 if bm1.at(x + a - 1, y - i) else -1
                ct += 1 if bm1.at(x - i, y + a) else -1
            if ct > 0:
                return True
            elif ct < 0:
                return False
        return False

    def _find_path(self, bm1, point):
        path = _Path()
        x = point.x
        y = point.y
        dirx = 0
        diry = 1
        path.sign = "+" if self.bm.at(point.x, point.y) else "-"

        while True:
            path.pt.append(_IPoint(x, y))
            if x > path.maxX:
                path.maxX = x
            if x < path.minX:
                path.minX = x
            if y > path.maxY:
                path.maxY = y
            if y < path.minY:
                path.minY = y
            path.len += 1

            x += dirx
            y += diry
            path.area -= x * diry

            if x == point.x and y == point.y:
                break

            # Integer division here truncates toward zero in C#; the
            # operands are even for the four cardinal directions, so // and
            # trunc agree, but _cdiv keeps it faithful regardless.
            l = bm1.at(x + _cdiv(dirx + diry - 1, 2),
                       y + _cdiv(diry - dirx - 1, 2))
            r = bm1.at(x + _cdiv(dirx - diry - 1, 2),
                       y + _cdiv(diry + dirx - 1, 2))

            if r and not l:
                tp = self.turnpolicy
                if (tp == "right"
                        or (tp == "black" and path.sign == "+")
                        or (tp == "white" and path.sign == "-")
                        or (tp == "majority" and self._majority(bm1, x, y))
                        or (tp == "minority" and not self._majority(bm1, x, y))):
                    tmp = dirx
                    dirx = -diry
                    diry = tmp
                else:
                    tmp = dirx
                    dirx = diry
                    diry = -tmp
            elif r:
                tmp = dirx
                dirx = -diry
                diry = tmp
            elif not l:
                tmp = dirx
                dirx = diry
                diry = -tmp
        return path

    def _xor_path(self, bm1, path):
        pt = path.pt
        y1 = pt[0].y
        length = path.len
        for i in range(1, length):
            x = pt[i].x
            y = pt[i].y
            if y != y1:
                minY = y1 if y1 < y else y
                maxX = path.maxX
                for j in range(x, maxX):
                    bm1.flip(j, minY)
                y1 = y

    def _bm_to_pathlist(self):
        bm1 = self.bm.copy()
        current = _IPoint(0, 0)
        found, current = self._find_next(bm1, current)
        while found:
            path = self._find_path(bm1, current)
            self._xor_path(bm1, path)
            if path.area > self.turdsize:
                self.pathlist.append(path)
            found, current = self._find_next(bm1, current)

    # --- stage 1: sums ---------------------------------------------------- #

    def _calc_sums(self, path):
        path.x0 = path.pt[0].x
        path.y0 = path.pt[0].y
        s = path.sums
        s.append(_Sum(0, 0, 0, 0, 0))
        for i in range(path.len):
            x = path.pt[i].x - path.x0
            y = path.pt[i].y - path.y0
            s.append(_Sum(s[i].x + x, s[i].y + y, s[i].xy + x * y,
                          s[i].x2 + x * x, s[i].y2 + y * y))

    # --- stage 2: optimal polygon ----------------------------------------- #

    def _penalty3(self, path, i, j):
        n = path.len
        pt = path.pt
        sums = path.sums
        r = 0
        if j >= n:
            j -= n
            r = 1
        if r == 0:
            x = sums[j + 1].x - sums[i].x
            y = sums[j + 1].y - sums[i].y
            x2 = sums[j + 1].x2 - sums[i].x2
            xy = sums[j + 1].xy - sums[i].xy
            y2 = sums[j + 1].y2 - sums[i].y2
            k = j + 1 - i
        else:
            x = sums[j + 1].x - sums[i].x + sums[n].x
            y = sums[j + 1].y - sums[i].y + sums[n].y
            x2 = sums[j + 1].x2 - sums[i].x2 + sums[n].x2
            xy = sums[j + 1].xy - sums[i].xy + sums[n].xy
            y2 = sums[j + 1].y2 - sums[i].y2 + sums[n].y2
            k = j + 1 - i + n

        px = (pt[i].x + pt[j].x) / 2.0 - pt[0].x
        py = (pt[i].y + pt[j].y) / 2.0 - pt[0].y
        # (ex,ey) is the edge normal: ey uses dx, ex uses -dy. Faithful.
        ey = (pt[j].x - pt[i].x)
        ex = -(pt[j].y - pt[i].y)

        a = ((x2 - 2 * x * px) / k + px * px)
        b = ((xy - x * py - y * px) / k + px * py)
        c = ((y2 - 2 * y * py) / k + py * py)

        s = ex * ex * a + 2 * ex * ey * b + ey * ey * c
        return math.sqrt(s)

    def _calc_lon(self, path):
        n = path.len
        pt = path.pt
        pivk = [0] * n
        nc = [0] * n
        ct = [0, 0, 0, 0]
        path.lon = [0] * n

        constraint = [_IPoint(), _IPoint()]
        cur = _IPoint()
        off = _IPoint()
        dk = _IPoint()

        k = 0
        for i in range(n - 1, -1, -1):
            if pt[i].x != pt[k].x and pt[i].y != pt[k].y:
                k = i + 1
            nc[i] = k

        for i in range(n - 1, -1, -1):
            ct[0] = ct[1] = ct[2] = ct[3] = 0
            dir = _cdiv(3 + 3 * (pt[_mod(i + 1, n)].x - pt[i].x)
                        + (pt[_mod(i + 1, n)].y - pt[i].y), 2)
            ct[dir] += 1

            constraint[0].x = 0
            constraint[0].y = 0
            constraint[1].x = 0
            constraint[1].y = 0

            k = nc[i]
            k1 = i
            foundk = 0
            while True:
                foundk = 0
                dir = _cdiv(3 + 3 * _sign(pt[k].x - pt[k1].x)
                            + _sign(pt[k].y - pt[k1].y), 2)
                ct[dir] += 1

                if ct[0] == 1 and ct[1] == 1 and ct[2] == 1 and ct[3] == 1:
                    pivk[i] = k1
                    foundk = 1
                    break

                cur.x = pt[k].x - pt[i].x
                cur.y = pt[k].y - pt[i].y

                if _xprodi(constraint[0], cur) < 0 or _xprodi(constraint[1], cur) > 0:
                    break

                if abs(cur.x) <= 1 and abs(cur.y) <= 1:
                    pass
                else:
                    off.x = cur.x + (1 if (cur.y >= 0 and (cur.y > 0 or cur.x < 0)) else -1)
                    off.y = cur.y + (1 if (cur.x <= 0 and (cur.x < 0 or cur.y < 0)) else -1)
                    if _xprodi(constraint[0], off) >= 0:
                        constraint[0].x = off.x
                        constraint[0].y = off.y
                    off.x = cur.x + (1 if (cur.y <= 0 and (cur.y < 0 or cur.x < 0)) else -1)
                    off.y = cur.y + (1 if (cur.x >= 0 and (cur.x > 0 or cur.y < 0)) else -1)
                    if _xprodi(constraint[1], off) <= 0:
                        constraint[1].x = off.x
                        constraint[1].y = off.y
                k1 = k
                k = nc[k1]
                if not _cyclic(k, i, k1):
                    break

            if foundk == 0:
                dk.x = _sign(pt[k].x - pt[k1].x)
                dk.y = _sign(pt[k].y - pt[k1].y)
                cur.x = pt[k1].x - pt[i].x
                cur.y = pt[k1].y - pt[i].y

                a = _xprodi(constraint[0], cur)
                b = _xprodi(constraint[0], dk)
                c = _xprodi(constraint[1], cur)
                d = _xprodi(constraint[1], dk)

                j = 10000000
                if b < 0:
                    j = _cdiv(a, -b)
                if d > 0:
                    j = min(j, _cdiv(-c, d))
                pivk[i] = _mod(k1 + j, n)

        j = pivk[n - 1]
        path.lon[n - 1] = j
        for i in range(n - 2, -1, -1):
            if _cyclic(i + 1, pivk[i], j):
                j = pivk[i]
            path.lon[i] = j

        i = n - 1
        while _cyclic(_mod(i + 1, n), j, path.lon[i]):
            path.lon[i] = j
            i -= 1

    def _best_polygon(self, path):
        n = path.len
        clip0 = [0] * n
        pen = [0.0] * (n + 1)
        prev = [0] * (n + 1)
        clip1 = [0] * (n + 1)
        seg0 = [0] * (n + 1)
        seg1 = [0] * (n + 1)

        for i in range(n):
            c = _mod(path.lon[_mod(i - 1, n)] - 1, n)
            if c == i:
                c = _mod(i + 1, n)
            if c < i:
                clip0[i] = n
            else:
                clip0[i] = c

        j = 1
        for i in range(n):
            while j <= clip0[i]:
                clip1[j] = i
                j += 1

        i = 0
        j = 0
        while i < n:
            seg0[j] = i
            i = clip0[i]
            j += 1
        seg0[j] = n
        m = j

        i = n
        for j in range(m, 0, -1):
            seg1[j] = i
            i = clip1[i]
        seg1[0] = 0

        pen[0] = 0
        for j in range(1, m + 1):
            for i in range(seg1[j], seg0[j] + 1):
                best = -1.0
                for k in range(seg0[j - 1], clip1[i] - 1, -1):
                    thispen = self._penalty3(path, k, i) + pen[k]
                    if best < 0 or thispen < best:
                        prev[i] = k
                        best = thispen
                pen[i] = best
        path.m = m
        path.po = [0] * m
        i = n
        for j in range(m - 1, -1, -1):
            i = prev[i]
            path.po[j] = i

    # --- stage 3: vertex adjustment --------------------------------------- #

    def _adjust_vertices(self, path):
        m = path.m
        po = path.po
        n = path.len
        pt = path.pt
        x0 = path.x0
        y0 = path.y0
        ctr = [None] * m
        dir = [None] * m
        q = [None] * m
        v = [0.0, 0.0, 0.0]
        s = _DPoint()
        path.curve = _Privcurve(m)

        for i in range(m):
            j = po[_mod(i + 1, m)]
            j = _mod(j - po[i], n) + po[i]
            ctr[i] = _DPoint()
            dir[i] = _DPoint()
            _pointslope(path, po[i], j, ctr[i], dir[i])

        for i in range(m):
            q[i] = _Quad()
            d = dir[i].X * dir[i].X + dir[i].Y * dir[i].Y
            if d == 0.0:
                for j in range(3):
                    for k in range(3):
                        q[i].data[j * 3 + k] = 0
            else:
                v[0] = dir[i].Y
                v[1] = -dir[i].X
                v[2] = -v[1] * ctr[i].Y - v[0] * ctr[i].X
                for l in range(3):
                    for k in range(3):
                        q[i].data[l * 3 + k] = v[l] * v[k] / d

        for i in range(m):
            Q = _Quad()
            w = _DPoint()
            s.X = pt[po[i]].x - x0
            s.Y = pt[po[i]].y - y0
            j = _mod(i - 1, m)
            for l in range(3):
                for k in range(3):
                    Q.data[l * 3 + k] = q[j].at(l, k) + q[i].at(l, k)

            while True:
                det = Q.at(0, 0) * Q.at(1, 1) - Q.at(0, 1) * Q.at(1, 0)
                if det != 0.0:
                    w.X = (-Q.at(0, 2) * Q.at(1, 1) + Q.at(1, 2) * Q.at(0, 1)) / det
                    w.Y = (Q.at(0, 2) * Q.at(1, 0) - Q.at(1, 2) * Q.at(0, 0)) / det
                    break
                # Singular: lines parallel. Add an orthogonal axis through
                # the center of the unit square and retry (must terminate).
                if Q.at(0, 0) > Q.at(1, 1):
                    v[0] = -Q.at(0, 1)
                    v[1] = Q.at(0, 0)
                elif Q.at(1, 1) != 0.0:
                    v[0] = -Q.at(1, 1)
                    v[1] = Q.at(1, 0)
                else:
                    v[0] = 1
                    v[1] = 0
                d = v[0] * v[0] + v[1] * v[1]
                v[2] = -v[1] * s.Y - v[0] * s.X
                for l in range(3):
                    for k in range(3):
                        Q.data[l * 3 + k] += v[l] * v[k] / d

            dx = abs(w.X - s.X)
            dy = abs(w.Y - s.Y)
            if dx <= 0.5 and dy <= 0.5:
                path.curve.vertex[i] = _DPoint(w.X + x0, w.Y + y0)
                continue

            # Minimum not in the unit square: minimize on the boundary.
            mn = _quadform(Q, s)
            xmin = s.X
            ymin = s.Y

            if Q.at(0, 0) != 0.0:
                for z in range(2):
                    w.Y = s.Y - 0.5 + z
                    w.X = -(Q.at(0, 1) * w.Y + Q.at(0, 2)) / Q.at(0, 0)
                    dx = abs(w.X - s.X)
                    cand = _quadform(Q, w)
                    if dx <= 0.5 and cand < mn:
                        mn = cand
                        xmin = w.X
                        ymin = w.Y

            if Q.at(1, 1) != 0.0:
                for z in range(2):
                    w.X = s.X - 0.5 + z
                    w.Y = -(Q.at(1, 0) * w.X + Q.at(1, 2)) / Q.at(1, 1)
                    dy = abs(w.Y - s.Y)
                    cand = _quadform(Q, w)
                    if dy <= 0.5 and cand < mn:
                        mn = cand
                        xmin = w.X
                        ymin = w.Y

            for l in range(2):
                for k in range(2):
                    w.X = s.X - 0.5 + l
                    w.Y = s.Y - 0.5 + k
                    cand = _quadform(Q, w)
                    if cand < mn:
                        mn = cand
                        xmin = w.X
                        ymin = w.Y

            path.curve.vertex[i] = _DPoint(xmin + x0, ymin + y0)

    # --- stage 4: smoothing ----------------------------------------------- #

    def _reverse(self, path):
        curve = path.curve
        m = curve.n
        v = curve.vertex
        i = 0
        j = m - 1
        while i < j:
            v[i], v[j] = v[j], v[i]
            i += 1
            j -= 1

    def _smooth(self, path):
        m = path.curve.n
        curve = path.curve
        if path.sign == "-":
            self._reverse(path)

        for i in range(m):
            j = _mod(i + 1, m)
            k = _mod(i + 2, m)
            p4 = _interval(1 / 2.0, curve.vertex[k], curve.vertex[j])

            denom = _ddenom(curve.vertex[i], curve.vertex[k])
            if denom != 0.0:
                dd = _dpara(curve.vertex[i], curve.vertex[j], curve.vertex[k]) / denom
                dd = abs(dd)
                alpha = (1 - 1.0 / dd) if dd > 1 else 0
                alpha = alpha / 0.75
            else:
                alpha = 4 / 3.0
            curve.alpha0[j] = alpha

            if alpha >= self.alphamax:
                curve.tag[j] = _POTRACE_CORNER
                curve.c[3 * j + 1] = curve.vertex[j]
                curve.c[3 * j + 2] = p4
            else:
                if alpha < 0.55:
                    alpha = 0.55
                elif alpha > 1:
                    alpha = 1
                p2 = _interval(0.5 + 0.5 * alpha, curve.vertex[i], curve.vertex[j])
                p3 = _interval(0.5 + 0.5 * alpha, curve.vertex[k], curve.vertex[j])
                curve.tag[j] = _POTRACE_CURVETO
                curve.c[3 * j + 0] = p2
                curve.c[3 * j + 1] = p3
                curve.c[3 * j + 2] = p4
            curve.alpha[j] = alpha
            curve.beta[j] = 0.5
        curve.alphacurve = 1

    # --- stage 5: curve optimization -------------------------------------- #

    def _opti_penalty(self, path, i, j, res, opttolerance, convc, areac):
        m = path.curve.n
        curve = path.curve
        vertex = curve.vertex

        if i == j:
            return 1

        k = i
        i1 = _mod(i + 1, m)
        k1 = _mod(k + 1, m)
        conv = convc[k1]
        if conv == 0:
            return 1
        d = _ddist(vertex[i], vertex[i1])
        k = k1
        while k != j:
            k1 = _mod(k + 1, m)
            k2 = _mod(k + 2, m)
            if convc[k1] != conv:
                return 1
            if _sign(_cprod(vertex[i], vertex[i1], vertex[k1], vertex[k2])) != conv:
                return 1
            if (_iprod1(vertex[i], vertex[i1], vertex[k1], vertex[k2])
                    < d * _ddist(vertex[k1], vertex[k2]) * -0.999847695156):
                return 1
            k = k1

        p0 = curve.c[_mod(i, m) * 3 + 2].copy()
        p1 = vertex[_mod(i + 1, m)].copy()
        p2 = vertex[_mod(j, m)].copy()
        p3 = curve.c[_mod(j, m) * 3 + 2].copy()

        area = areac[j] - areac[i]
        area -= _dpara(vertex[0], curve.c[i * 3 + 2], curve.c[j * 3 + 2]) / 2
        if i >= j:
            area += areac[m]

        A1 = _dpara(p0, p1, p2)
        A2 = _dpara(p0, p1, p3)
        A3 = _dpara(p0, p2, p3)
        A4 = A1 + A3 - A2

        if A2 == A1:
            return 1

        t = A3 / (A3 - A4)
        s = A2 / (A2 - A1)
        A = A2 * t / 2.0

        if A == 0.0:
            return 1

        R = area / A
        alpha = 2 - math.sqrt(4 - R / 0.3)

        res.c[0] = _interval(t * alpha, p0, p1)
        res.c[1] = _interval(s * alpha, p3, p2)
        res.alpha = alpha
        res.t = t
        res.s = s

        p1 = res.c[0].copy()
        p2 = res.c[1].copy()

        res.pen = 0
        k = _mod(i + 1, m)
        while k != j:
            k1 = _mod(k + 1, m)
            t = _tangent(p0, p1, p2, p3, vertex[k], vertex[k1])
            if t < -0.5:
                return 1
            ptt = _bezier3(t, p0, p1, p2, p3)
            d = _ddist(vertex[k], vertex[k1])
            if d == 0.0:
                return 1
            d1 = _dpara(vertex[k], vertex[k1], ptt) / d
            if abs(d1) > opttolerance:
                return 1
            if (_iprod(vertex[k], vertex[k1], ptt) < 0
                    or _iprod(vertex[k1], vertex[k], ptt) < 0):
                return 1
            res.pen += d1 * d1
            k = k1

        k = i
        while k != j:
            k1 = _mod(k + 1, m)
            t = _tangent(p0, p1, p2, p3, curve.c[k * 3 + 2], curve.c[k1 * 3 + 2])
            if t < -0.5:
                return 1
            ptt = _bezier3(t, p0, p1, p2, p3)
            d = _ddist(curve.c[k * 3 + 2], curve.c[k1 * 3 + 2])
            if d == 0.0:
                return 1
            d1 = _dpara(curve.c[k * 3 + 2], curve.c[k1 * 3 + 2], ptt) / d
            d2 = _dpara(curve.c[k * 3 + 2], curve.c[k1 * 3 + 2], vertex[k1]) / d
            d2 *= 0.75 * curve.alpha[k1]
            if d2 < 0:
                d1 = -d1
                d2 = -d2
            if d1 < d2 - opttolerance:
                return 1
            if d1 < d2:
                res.pen += (d1 - d2) * (d1 - d2)
            k = k1

        return 0

    def _opti_curve(self, path):
        curve = path.curve
        m = curve.n
        vert = curve.vertex
        pt = [0] * (m + 1)
        pen = [0.0] * (m + 1)
        length = [0] * (m + 1)
        opt = [None] * (m + 1)
        o = _Opti()
        convc = [0] * m
        areac = [0.0] * (m + 1)

        for i in range(m):
            if curve.tag[i] == _POTRACE_CURVETO:
                convc[i] = _sign(_dpara(vert[_mod(i - 1, m)], vert[i],
                                        vert[_mod(i + 1, m)]))
            else:
                convc[i] = 0

        area = 0.0
        areac[0] = 0.0
        p0 = curve.vertex[0]
        for i in range(m):
            i1 = _mod(i + 1, m)
            if curve.tag[i1] == _POTRACE_CURVETO:
                alpha = curve.alpha[i1]
                area += (0.3 * alpha * (4 - alpha)
                         * _dpara(curve.c[i * 3 + 2], vert[i1], curve.c[i1 * 3 + 2]) / 2)
                area += _dpara(p0, curve.c[i * 3 + 2], curve.c[i1 * 3 + 2]) / 2
            areac[i + 1] = area

        pt[0] = -1
        pen[0] = 0
        length[0] = 0

        for j in range(1, m + 1):
            pt[j] = j - 1
            pen[j] = pen[j - 1]
            length[j] = length[j - 1] + 1
            for i in range(j - 2, -1, -1):
                r = self._opti_penalty(path, i, _mod(j, m), o, self.opttolerance,
                                       convc, areac)
                if r == 1:
                    break
                if (length[j] > length[i] + 1
                        or (length[j] == length[i] + 1 and pen[j] > pen[i] + o.pen)):
                    pt[j] = i
                    pen[j] = pen[i] + o.pen
                    length[j] = length[i] + 1
                    opt[j] = o
                    o = _Opti()

        om = length[m]
        ocurve = _Privcurve(om)
        s = [0.0] * om
        t = [0.0] * om

        j = m
        for i in range(om - 1, -1, -1):
            if pt[j] == j - 1:
                ocurve.tag[i] = curve.tag[_mod(j, m)]
                ocurve.c[i * 3 + 0] = curve.c[_mod(j, m) * 3 + 0]
                ocurve.c[i * 3 + 1] = curve.c[_mod(j, m) * 3 + 1]
                ocurve.c[i * 3 + 2] = curve.c[_mod(j, m) * 3 + 2]
                ocurve.vertex[i] = curve.vertex[_mod(j, m)]
                ocurve.alpha[i] = curve.alpha[_mod(j, m)]
                ocurve.alpha0[i] = curve.alpha0[_mod(j, m)]
                ocurve.beta[i] = curve.beta[_mod(j, m)]
                s[i] = t[i] = 1.0
            else:
                ocurve.tag[i] = _POTRACE_CURVETO
                ocurve.c[i * 3 + 0] = opt[j].c[0]
                ocurve.c[i * 3 + 1] = opt[j].c[1]
                ocurve.c[i * 3 + 2] = curve.c[_mod(j, m) * 3 + 2]
                ocurve.vertex[i] = _interval(opt[j].s, curve.c[_mod(j, m) * 3 + 2],
                                             vert[_mod(j, m)])
                ocurve.alpha[i] = opt[j].alpha
                ocurve.alpha0[i] = opt[j].alpha
                s[i] = opt[j].s
                t[i] = opt[j].t
            j = pt[j]

        for i in range(om):
            i1 = _mod(i + 1, om)
            ocurve.beta[i] = s[i] / (s[i] + t[i1])
        ocurve.alphacurve = 1
        path.curve = ocurve

    # --- emit ------------------------------------------------------------- #

    def _trace_to_list(self):
        result = []
        for P in self.pathlist:
            curve_list = []
            result.append(curve_list)
            n = P.curve.n
            L = P.curve.c[(n - 1) * 3 + 2]
            for j in range(n):
                A = P.curve.c[j * 3 + 1]
                B = P.curve.c[j * 3 + 2]
                if P.curve.tag[j] == _POTRACE_CORNER:
                    curve_list.append(_Curve(_LINE, L, L, A, A))
                    curve_list.append(_Curve(_LINE, A, A, B, B))
                else:
                    CP = P.curve.c[j * 3]
                    curve_list.append(_Curve(_BEZIER, L, CP, A, B))
                L = B
        return result

    def trace(self, w, h, data):
        """Run the full Potrace pipeline on a binary bitmap (``data`` is a
        bytearray of 0/1, row-major). Returns a list of contours, each a
        list of _Curve in pixel coordinates."""
        self.bm = _Bitmap(w, h, data)
        self.pathlist = []
        self._bm_to_pathlist()
        for path in self.pathlist:
            self._calc_sums(path)
            self._calc_lon(path)
            self._best_polygon(path)
            self._adjust_vertices(path)
            self._smooth(path)
            if self.curveoptimizing:
                self._opti_curve(path)
        return self._trace_to_list()


# --------------------------------------------------------------------------- #
# SECTION B — cubic Bezier -> biarc geometry (port of BezierToBiarc/*)
# --------------------------------------------------------------------------- #
#
# LaserGRBL uses 32-bit float Vector2 here; this port uses Python double.
# Practical parity, not bit-exact (see module docstring).


class _Vec2:
    __slots__ = ("X", "Y")

    def __init__(self, X=0.0, Y=0.0):
        self.X = X
        self.Y = Y

    def __add__(self, o):
        return _Vec2(self.X + o.X, self.Y + o.Y)

    def __sub__(self, o):
        return _Vec2(self.X - o.X, self.Y - o.Y)

    def __mul__(self, s):
        return _Vec2(self.X * s, self.Y * s)

    __rmul__ = __mul__

    def __truediv__(self, s):
        return _Vec2(self.X / s, self.Y / s)

    def length(self):
        return math.sqrt(self.X * self.X + self.Y * self.Y)

    @staticmethod
    def distance(a, b):
        return (a - b).length()


class _GLine:
    """Point-slope line (BezierToBiarc/Line.cs). m = nan for vertical."""

    __slots__ = ("m", "P")

    def __init__(self, P, m):
        self.P = P
        self.m = m

    @staticmethod
    def from_points(P1, P2):
        return _GLine(P1, _GLine._slope(P1, P2))

    @staticmethod
    def _slope(P1, P2):
        if P2.X == P1.X:
            return float("nan")
        return (P2.Y - P1.Y) / (P2.X - P1.X)

    def intersection(self, l):
        if math.isnan(self.m):
            return _GLine._vertical_intersection(self, l)
        elif math.isnan(l.m):
            return _GLine._vertical_intersection(l, self)
        else:
            x = (self.m * self.P.X - l.m * l.P.X - self.P.Y + l.P.Y) / (self.m - l.m)
            y = self.m * x - self.m * self.P.X + self.P.Y
            return _Vec2(x, y)

    @staticmethod
    def _vertical_intersection(vl, l):
        x = vl.P.X
        y = l.m * (x - l.P.X) + l.P.Y
        return _Vec2(x, y)

    @staticmethod
    def create_perpendicular_at(P, P1):
        m = _GLine._slope(P, P1)
        if m == 0:
            return _GLine(P, float("nan"))
        elif math.isnan(m):
            return _GLine(P, 0.0)
        else:
            return _GLine(P, -1.0 / m)


class _Arc:
    __slots__ = ("C", "r", "startAngle", "sweepAngle", "P1", "P2")

    def __init__(self, C, r, startAngle, sweepAngle, P1, P2):
        self.C = C
        self.r = r
        self.startAngle = startAngle
        self.sweepAngle = sweepAngle
        self.P1 = P1
        self.P2 = P2

    @property
    def is_clockwise(self):
        return self.sweepAngle > 0

    def point_at(self, t):
        x = self.C.X + self.r * math.cos(self.startAngle + t * self.sweepAngle)
        y = self.C.Y + self.r * math.sin(self.startAngle + t * self.sweepAngle)
        return _Vec2(x, y)

    @property
    def length(self):
        return self.r * abs(self.sweepAngle)

    @property
    def linear_length(self):
        # Faithful to the source bug (Arc.cs:75): dY = P2.Y - P2.Y = 0, so
        # this is |P2.X - P1.X|, not the Euclidean chord. CasoLimite relies
        # on it; replicate to decide arc-vs-line the same way LaserGRBL does.
        dX = self.P2.X - self.P1.X
        dY = self.P2.Y - self.P2.Y
        return math.sqrt(dX * dX + dY * dY)


class _BiArc:
    __slots__ = ("A1", "A2")

    def __init__(self, P1, T1, P2, T2, T):
        # Orientation (curve winding) via shoelace sum over P1, T, P2.
        s = 0.0
        s += (T.X - P1.X) * (T.Y + P1.Y)
        s += (P2.X - T.X) * (P2.Y + T.Y)
        s += (P1.X - P2.X) * (P1.Y + P2.Y)
        cw = s < 0

        tl1 = _GLine.create_perpendicular_at(P1, P1 + T1)
        tl2 = _GLine.create_perpendicular_at(P2, P2 + T2)

        P1T2 = (P1 + T) / 2
        pbP1T = _GLine.create_perpendicular_at(P1T2, T)
        P2T2 = (P2 + T) / 2
        pbP2T = _GLine.create_perpendicular_at(P2T2, T)

        C1 = tl1.intersection(pbP1T)
        C2 = tl2.intersection(pbP2T)

        r1 = (C1 - P1).length()
        r2 = (C2 - P2).length()

        start_vector1 = P1 - C1
        end_vector1 = T - C1
        start_angle1 = math.atan2(start_vector1.Y, start_vector1.X)
        sweep_angle1 = math.atan2(end_vector1.Y, end_vector1.X) - start_angle1

        start_vector2 = T - C2
        end_vector2 = P2 - C2
        start_angle2 = math.atan2(start_vector2.Y, start_vector2.X)
        sweep_angle2 = math.atan2(end_vector2.Y, end_vector2.X) - start_angle2

        if cw and sweep_angle1 < 0:
            sweep_angle1 = 2 * math.pi + sweep_angle1
        if not cw and sweep_angle1 > 0:
            sweep_angle1 = sweep_angle1 - 2 * math.pi
        if cw and sweep_angle2 < 0:
            sweep_angle2 = 2 * math.pi + sweep_angle2
        if not cw and sweep_angle2 > 0:
            sweep_angle2 = sweep_angle2 - 2 * math.pi

        self.A1 = _Arc(C1, r1, start_angle1, sweep_angle1, P1, T)
        self.A2 = _Arc(C2, r2, start_angle2, sweep_angle2, T, P2)

    def point_at(self, t):
        s = self.A1.length / (self.A1.length + self.A2.length)
        if t <= s:
            return self.A1.point_at(t / s)
        return self.A2.point_at((t - s) / (1 - s))

    @property
    def length(self):
        return self.A1.length + self.A2.length


class _CubicBezier:
    __slots__ = ("P1", "P2", "C1", "C2")

    def __init__(self, P1, C1, C2, P2):
        # Faithful to the source's odd field-assignment order.
        self.P1 = P1
        self.C1 = C1
        self.P2 = P2
        self.C2 = C2

    def point_at(self, t):
        s = 1 - t
        return (self.P1 * (s ** 3) + self.C1 * (3 * s * s * t)
                + self.C2 * (3 * s * t * t) + self.P2 * (t ** 3))

    def split(self, t):
        p0 = self.P1 + (self.C1 - self.P1) * t
        p1 = self.C1 + (self.C2 - self.C1) * t
        p2 = self.C2 + (self.P2 - self.C2) * t
        p01 = p0 + (p1 - p0) * t
        p12 = p1 + (p2 - p1) * t
        dp = p01 + (p12 - p01) * t
        return (_CubicBezier(self.P1, p0, p01, dp),
                _CubicBezier(dp, p12, p2, self.P2))

    @property
    def inflexion_points(self):
        A = self.C1 - self.P1
        B = self.C2 - self.C1 - A
        C = self.P2 - self.C2 - A - B * 2

        a = complex(B.X * C.Y - B.Y * C.X, 0)
        b = complex(A.X * C.Y - A.Y * C.X, 0)
        c = complex(A.X * B.Y - A.Y * B.X, 0)

        # Guard against a == 0 (Python complex division would raise; C#'s
        # Complex returns NaN/inf which then fail IsRealInflexionPoint).
        disc = _csqrt(b * b - 4 * a * c)
        if a == 0:
            return (complex(float("nan"), 0), complex(float("nan"), 0))
        t1 = (-b + disc) / (2 * a)
        t2 = (-b - disc) / (2 * a)
        return (t1, t2)


def _csqrt(z):
    """Complex square root, principal branch (matches C# Complex.Sqrt =
    FromPolarCoordinates(sqrt(magnitude), phase/2))."""
    import cmath
    return cmath.sqrt(z)


def _is_real_inflexion(t):
    return t.imag == 0 and t.real > 0 and t.real < 1


def _approx_cubic_bezier(bezier, sampling_step, tolerance):
    """Approximate a cubic Bezier with biarcs (BezierToBiarc/Algorithm.cs).
    Returns a list of _BiArc, or None if a sub-curve is too long
    (nrPointsToCheck > 1000), in which case the caller emits a straight
    line fallback."""
    biarcs = []
    # Python list as a LIFO stack (append/pop), matching C#'s Stack.
    curves = [bezier]

    to_split = curves.pop()
    inflex = to_split.inflexion_points
    i1 = _is_real_inflexion(inflex[0])
    i2 = _is_real_inflexion(inflex[1])

    if i1 and not i2:
        s = to_split.split(inflex[0].real)
        curves.append(s[1])
        curves.append(s[0])
    elif not i1 and i2:
        s = to_split.split(inflex[1].real)
        curves.append(s[1])
        curves.append(s[0])
    elif i1 and i2:
        t1 = inflex[0].real
        t2 = inflex[1].real
        if t1 > t2:
            t1, t2 = t2, t1
        s1 = to_split.split(t1)
        t2 = (1 - t1) * t2
        to_split = s1[1]
        s2 = to_split.split(t2)
        curves.append(s2[1])
        curves.append(s2[0])
        curves.append(s1[0])
    else:
        curves.append(to_split)

    while curves:
        bezier = curves.pop()

        t1 = _GLine.from_points(bezier.P1, bezier.C1)
        t2 = _GLine.from_points(bezier.P2, bezier.C2)
        V = t1.intersection(t2)

        dP2V = _Vec2.distance(bezier.P2, V)
        dP1V = _Vec2.distance(bezier.P1, V)
        dP1P2 = _Vec2.distance(bezier.P1, bezier.P2)
        G = (bezier.P1 * dP2V + bezier.P2 * dP1V + V * dP1P2) / (dP2V + dP1V + dP1P2)

        biarc = _BiArc(bezier.P1, bezier.P1 - bezier.C1, bezier.P2,
                       bezier.P2 - bezier.C2, G)

        nr = biarc.length / sampling_step
        if nr > 1000:
            return None
        parameter_step = 1.0 / nr

        max_distance = 0.0
        max_distance_at = 0.0
        i = 0
        while i <= nr:
            t = parameter_step * i
            u1 = biarc.point_at(t)
            u2 = bezier.point_at(t)
            distance = (u1 - u2).length()
            if distance > max_distance:
                max_distance = distance
                max_distance_at = t
            i += 1

        if max_distance > tolerance:
            bs = bezier.split(max_distance_at)
            curves.append(bs[1])
            curves.append(bs[0])
        else:
            biarcs.append(biarc)

    return biarcs


# --------------------------------------------------------------------------- #
# SECTION C — image preprocessing to a binary bitmap (Vectorize path)
# --------------------------------------------------------------------------- #
#
# Port of the Vectorize branch of ImageProcessor.cs + ImageTransform.cs.
# Done with Pillow band ops (no per-pixel Python loop) for speed; the result
# is identical because the gray image has R=G=B and Potrace's final
# binarization is R+G+B < 382.5, i.e. gray < 127.5.

_GRAY_MAXDIFF = 20
_GRAY_SAMPLE_STEP = 10

_FORMULA_WEIGHTS = {
    "simple_average": (0.333, 0.333, 0.333),
    "weight_average": (0.333, 0.444, 0.222),
    "optical_correct": (0.299, 0.587, 0.114),
}


def _is_grayscale(img):
    """LaserGRBL's TestGrayScale: sample every 10th pixel; the image is
    grayscale if every sample's max channel spread is < 20."""
    if img.mode in ("L", "LA", "1"):
        return True
    rgb = img.convert("RGB")
    px = rgb.load()
    for y in range(0, img.height, _GRAY_SAMPLE_STEP):
        for x in range(0, img.width, _GRAY_SAMPLE_STEP):
            r, g, b = px[x, y]
            if max(r, g, b) - min(r, g, b) >= _GRAY_MAXDIFF:
                return False
    return True


def _preprocess(image_path, profile):
    """Open, resize and binarize an image into Potrace's input bitmap.

    Returns (w, h, data) where data is a bytearray of 0/1 (1 = black,
    row-major), already flipped vertically like LaserGRBL does before
    tracing.
    """
    from pathlib import Path

    from PIL import Image

    if not Path(image_path).exists():
        raise FileNotFoundError(image_path)

    img = Image.open(image_path)
    img = img.convert("RGBA")

    # Target pixel size: width drives it, height follows the aspect ratio
    # (LaserGRBL's TargetSize keeps the ratio locked). res = px per mm.
    res = profile.quality
    px_w = max(1, int(round(profile.width_mm * res)))
    px_h = max(1, int(round(profile.width_mm * (img.height / img.width) * res)))

    if (px_w, px_h) != img.size:
        img = img.resize((px_w, px_h), Image.BICUBIC)

    # --- grayscale (ColorMatrix): out = fr*R + fg*G + fb*B + bias --------- #
    formula = profile.formula
    if formula != "custom" and _is_grayscale(img):
        formula = "simple_average"  # LaserGRBL forces this for gray inputs

    if formula == "custom":
        base = (0.333 * (profile.red / 100.0),
                0.333 * (profile.green / 100.0),
                0.333 * (profile.blue / 100.0))
    else:
        base = _FORMULA_WEIGHTS[formula]

    contrast = profile.contrast / 100.0
    fr, fg, fb = (w * contrast for w in base)
    bias = -((100 - profile.brightness) / 100.0) * 255.0

    rgb = img.convert("RGB")
    # Pillow convert("L", matrix): L = R*a + G*b + B*c + d, clamped to byte.
    gray = rgb.convert("L", (fr, fg, fb, bias))
    alpha = img.getchannel("A")

    # --- whitenize (white-clip): near-white pixels become transparent ----- #
    clip = profile.white_clip
    if clip > 0:
        # Faithful condition is strict: sourceX > 255 - clip (and < 255 +
        # clip, always true for a byte). Mark those pixels alpha = 0.
        wmask = gray.point(lambda v, c=clip: 255 if v > 255 - c else 0)
        zeros = Image.new("L", gray.size, 0)
        alpha = Image.composite(zeros, alpha, wmask)

    # --- threshold: flatten over white, then optional per-channel cut ----- #
    white = Image.new("L", gray.size, 255)
    flat = Image.composite(gray, white, alpha)  # gray where alpha=255, white where 0

    if profile.use_threshold:
        thr = profile.threshold / 100.0 * 255.0
        flat = flat.point(lambda v, t=thr: 255 if v >= t else 0)

    # --- flip Y (LaserGRBL flips the bitmap before tracing) --------------- #
    flat = flat.transpose(Image.FLIP_TOP_BOTTOM)

    # --- Potrace's ConvertBitmap: black (1) if R+G+B < 382.5, i.e. gray<127.5
    bit = flat.point(lambda v: 1 if v < 127.5 else 0)
    data = bytearray(bit.tobytes())
    return px_w, px_h, data


# --------------------------------------------------------------------------- #
# SECTION D — G-code emitter (port of CsPotraceExportGCODE.cs)
# --------------------------------------------------------------------------- #

# ApproxCubicBezier sampling parameters, in pixel units (LaserGRBL's
# Export2GCode passes ApproxCubicBezier(cb, 5, 2)).
_BIARC_SAMPLING_STEP = 5.0
_BIARC_TOLERANCE = 2.0


def _gc_num(number, scale):
    """formatnumber: pixel coordinate / scale -> mm, formatted "0.###"."""
    return _fmt_num(number / scale)


def _caso_limite(arc, scale):
    """CasoLimite: degrade an arc to a straight G1 if it is degenerate or
    nearly straight."""
    if arc.r == 0 or arc.linear_length == 0:
        return True
    if (arc.linear_length / scale) <= 0.5:
        return True
    if arc.r / arc.linear_length > 1000:
        return True
    return False


def _arc_gc(arc, oX, oY, scale):
    if not _caso_limite(arc, scale):
        gnr = 2 if not arc.is_clockwise else 3
        return (f"G{gnr} X{_gc_num(arc.P2.X + oX, scale)} "
                f"Y{_gc_num(arc.P2.Y + oY, scale)} "
                f"I{_gc_num(arc.C.X - arc.P1.X, scale)} "
                f"J{_gc_num(arc.C.Y - arc.P1.Y, scale)}")
    return f"G1 X{_gc_num(arc.P2.X + oX, scale)} Y{_gc_num(arc.P2.Y + oY, scale)}"


def _segment_gc(curve, oX, oY, scale, out):
    if math.isnan(curve.linear_length):
        return

    if curve.kind == _LINE:
        out.append(f"G1 X{_gc_num(curve.B.X + oX, scale)} "
                   f"Y{_gc_num(curve.B.Y + oY, scale)}")
        return

    # Bezier -> biarcs -> G2/G3 (or G1 fallback).
    cb = _CubicBezier(
        _Vec2(curve.A.X, curve.A.Y),
        _Vec2(curve.ControlPointA.X, curve.ControlPointA.Y),
        _Vec2(curve.ControlPointB.X, curve.ControlPointB.Y),
        _Vec2(curve.B.X, curve.B.Y),
    )
    try:
        bal = _approx_cubic_bezier(cb, _BIARC_SAMPLING_STEP, _BIARC_TOLERANCE)
        if bal is not None:
            for ba in bal:
                if not math.isnan(ba.A1.length) and not math.isnan(ba.A1.linear_length):
                    out.append(_arc_gc(ba.A1, oX, oY, scale))
                if not math.isnan(ba.A2.length) and not math.isnan(ba.A2.linear_length):
                    out.append(_arc_gc(ba.A2, oX, oY, scale))
        else:
            out.append(f"G1 X{_gc_num(curve.B.X + oX, scale)} "
                       f"Y{_gc_num(curve.B.Y + oY, scale)}")
    except (ValueError, ZeroDivisionError):
        out.append(f"G1 X{_gc_num(curve.B.X + oX, scale)} "
                   f"Y{_gc_num(curve.B.Y + oY, scale)}")


def _export_gcode(contours, oX, oY, scale, l_on, l_off, skipcmd):
    """Export2GCode: walk each contour emitting rapid -> laser on ->
    segments -> laser off. oX/oY arrive premultiplied by scale (the offset
    stays in mm because _gc_num divides everything by scale)."""
    out = []
    for curves in contours:
        if not curves:
            continue
        # OnPathBegin: rapid to the first segment's start, then laser on.
        first = curves[0]
        out.append(f"{skipcmd} X{_gc_num(first.A.X + oX, scale)} "
                   f"Y{_gc_num(first.A.Y + oY, scale)}")
        out.append(l_on)
        for curve in curves:
            _segment_gc(curve, oX, oY, scale, out)
        out.append(l_off)
    return out


# --------------------------------------------------------------------------- #
# Public entry point (called from pygrbl_build.__init__)
# --------------------------------------------------------------------------- #


def convert(image_path, profile):
    """Trace an image to vector G-code and return the body as a list of
    lines (no header/footer, no trailing newlines)."""
    w, h, data = _preprocess(image_path, profile)

    tracer = _Potrace(
        turnpolicy=profile.turnpolicy,
        turdsize=profile.turdsize,
        alphamax=profile.alphamax,
        opttolerance=profile.opttolerance,
        curveoptimizing=profile.opticurve,
    )
    contours = tracer.trace(w, h, data)

    scale = profile.quality  # px per mm; _gc_num divides by it
    oX = profile.offset_x * scale
    oY = profile.offset_y * scale

    if profile.support_pwm:
        l_on = f"S{profile.s_max}"
        l_off = "S0"
    else:
        l_on = profile.laser_on
        l_off = profile.laser_off

    return _export_gcode(contours, oX, oY, scale, l_on, l_off, "G0")
