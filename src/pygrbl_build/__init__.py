import hashlib
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Iterable, Iterator, Optional, Tuple

from PIL import Image

from . import _l2l_native
from . import _svg
from . import _img2vec

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "L2LProfile",
    "l2l_gcode",
    "SvgProfile",
    "svg_gcode",
    "Img2VectorProfile",
    "img2vector_gcode",
    "write_gcode",
]

# Pixels are "visually gray" when their RGB channels differ less than
# this. Same rule and value as LaserGRBL's TestGrayScale.
_GRAY_MAXDIFF = 20
_GRAY_SAMPLE_STEP = 10


@dataclass(frozen=True)
class L2LProfile:
    """Line-to-Line raster calibration: one material/machine recipe for
    the l2l_gcode algorithm. Frozen and validated: a bad value blows up
    on your laptop at construction time, never in the G-code.

    Each algorithm in the library carries its own *Profile; this one
    holds everything l2l_gcode needs.

    An L2LProfile is the valuable part of your workshop in 10 lines:
    name it, version it, share it.

    Attributes:
        width_mm: Physical width of the engraving in mm. Height follows
            the image's aspect ratio.
        lines_per_mm: Resolution, vertical and horizontal (LaserGRBL's
            "Quality"). 10 lines/mm is roughly 254 DPI.
        feed: Engraving feed rate in mm/min.
        s_min: Laser power (S) for the lightest non-white gray.
        s_max: Laser power (S) for pure black, relative to your $30.
        white_threshold: Grayscale value at or above which a pixel is
            white: skipped entirely, beam off. 250 replicates
            LaserGRBL's WhiteClip=5.
        overscan_mm: Extra travel past row ends with the beam off, to
            accelerate/decelerate outside the ink. 0 (default) is
            LaserGRBL-faithful — it has no overscan; >0 is this
            library's optional improvement.
        bidirectional: Serpentine scan. False scans always
            left-to-right.
        invert: Engrave the negative.
        laser_on: "M4" for dynamic power (requires $32=1) or "M3" for
            constant power.

    Raises:
        TypeError: On construction, if a field has the wrong type.
        ValueError: On construction, if width_mm, lines_per_mm or feed
            are not positive, powers are negative or s_min > s_max,
            white_threshold is outside 1-255, overscan_mm is negative,
            or laser_on is not "M3"/"M4".
    """

    width_mm: float
    lines_per_mm: float = 10.0
    feed: int = 3000
    s_min: int = 0
    s_max: int = 1000
    white_threshold: int = 250
    overscan_mm: float = 0.0
    bidirectional: bool = True
    invert: bool = False
    laser_on: str = "M4"

    def __post_init__(self) -> None:
        for name in ("width_mm", "lines_per_mm", "overscan_mm"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number, got {type(value).__name__}")
        for name in ("feed", "s_min", "s_max", "white_threshold"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
        for name in ("bidirectional", "invert"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")

        if self.width_mm <= 0:
            raise ValueError(f"width_mm must be positive, got {self.width_mm}")
        if self.lines_per_mm <= 0:
            raise ValueError(f"lines_per_mm must be positive, got {self.lines_per_mm}")
        if self.feed <= 0:
            raise ValueError(f"feed must be positive, got {self.feed}")
        if self.s_min < 0:
            raise ValueError(f"s_min must be >= 0, got {self.s_min}")
        if self.s_max < self.s_min:
            raise ValueError(f"s_max ({self.s_max}) must be >= s_min ({self.s_min})")
        if not 1 <= self.white_threshold <= 255:
            raise ValueError(
                f"white_threshold must be in 1-255, got {self.white_threshold}"
            )
        if self.overscan_mm < 0:
            raise ValueError(f"overscan_mm must be >= 0, got {self.overscan_mm}")
        if self.laser_on not in ("M3", "M4"):
            raise ValueError(f"laser_on must be 'M3' or 'M4', got {self.laser_on!r}")


def _ensure_visually_gray(img: Image.Image, image_path: str) -> None:
    """Reject color images, with LaserGRBL's own test.

    Samples every 10th pixel; if any sample's RGB channels differ by 20
    or more, the image is color. Color conversion is your editor's job
    (it does it better than any formula here), and a grayscale-only
    contract removes the last possible preprocessing divergence with
    LaserGRBL.

    Raises:
        ValueError: If the image is color.
    """
    if img.mode in ("L", "LA", "1"):
        return
    rgb = img.convert("RGB").load()
    for y in range(0, img.height, _GRAY_SAMPLE_STEP):
        for x in range(0, img.width, _GRAY_SAMPLE_STEP):
            r, g, b = rgb[x, y]
            maxdiff = max(r, g, b) - min(r, g, b)
            if maxdiff >= _GRAY_MAXDIFF:
                raise ValueError(
                    f"{image_path!r} is a color image (max channel difference "
                    f"{maxdiff} >= {_GRAY_MAXDIFF}). Convert it to grayscale in "
                    "your image editor first: color conversion is editing work, "
                    "and your editor does it better."
                )


def _open_resized(image_path: str, p: L2LProfile) -> Tuple[Image.Image, bool]:
    """Open, validate and resize.

    The BICUBIC resize is the project's fidelity boundary. Returns the
    resized image (mode "L" or "LA") and whether it carries alpha.

    Raises:
        FileNotFoundError: If image_path does not exist.
        ValueError: If the image is color (see _ensure_visually_gray).
    """
    img = Image.open(image_path)
    _ensure_visually_gray(img, image_path)

    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )
    # Keep alpha THROUGH the resize, like LaserGRBL (it resizes before
    # GetColor).
    img = img.convert("LA" if has_alpha else "L")

    px_w = max(1, round(p.width_mm * p.lines_per_mm))
    px_h = max(1, round(px_w * img.height / img.width))
    # When the canvas already matches the profile (target == source),
    # skip the resample entirely: identity guaranteed by construction,
    # not inherited from Pillow's degenerate-filter behavior. LaserGRBL
    # is in the same degenerate case there (TargetSize×Quality), so
    # parity holds on both sides.
    if (px_w, px_h) != img.size:
        img = img.resize((px_w, px_h), Image.BICUBIC)
    return img, has_alpha


def _file_sha256(path: str, chars: int = 12) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:chars]


def _gcode_header(image_path: str, profile: L2LProfile, w: int, h: int) -> list:
    """The traceability/setup preamble: library version, image hash,
    the complete profile and the modal setup, so any engraved piece can
    be traced back to its exact recipe.
    """
    return [
        f"; pygrbl_build v{__version__}",
        f"; image: {Path(image_path).name} sha256:{_file_sha256(image_path)}",
        f"; profile: {profile}",
        f"; {w}x{h} px @ {profile.lines_per_mm} lines/mm",
        "G90",
        "G21",
        f"{profile.laser_on} S0",
        f"G1 F{profile.feed}",
    ]


def l2l_gcode(image_path: str, profile: L2LProfile) -> Iterator[str]:
    """Generate Line-to-Line raster G-code, line by line.

    Lazy generator: pairs naturally with pygrbl_streamer's stream() —
    the full G-code never needs to exist in memory or on disk. Pillow
    opens and resizes (the fidelity boundary), Python preformats every
    numeric string table and the header, and the _l2l_native C engine
    does pixel->power mapping, RLE, serpentine and line assembly. C
    never formats a number — float-formatting parity is structural,
    not coincidental. The image loads at call time; lines are produced
    lazily.

    Output: absolute coordinates (G90), millimeters (G21), origin at
    the engraving's bottom-left, Y growing upward (image top row =
    highest Y). Serpentine scan with run-length-encoded power segments;
    fully blank rows are skipped entirely.

    Args:
        image_path: Path to a grayscale (or B/W) image. Color images
            are rejected — convert in your editor first. Transparency
            is honored: transparent = blank.
        profile: The calibration to engrave with.

    Returns:
        Iterator of G-code lines, without trailing newlines.

    Raises:
        FileNotFoundError: If image_path does not exist.
        ValueError: If the image is color.
    """
    img, has_alpha = _open_resized(image_path, profile)
    w, h = img.size
    px = 1.0 / profile.lines_per_mm

    # Tuples, not lists: the C iterator caches pointers into these
    # strings and can outlive this frame by hours (it feeds a serial
    # port at engraving speed). Immutable containers make the cached
    # pointers structurally safe.
    XS = tuple(f"{i * px:.3f}" for i in range(w + 1))
    YS = tuple(f"{i * px:.3f}" for i in range(h))
    SS = tuple(str(v) for v in range(max(profile.s_max, 1) + 1))
    overscan = profile.overscan_mm
    if overscan > 0:
        XM: Optional[tuple] = tuple(f"{i * px - overscan:.3f}" for i in range(w + 1))
        XP: Optional[tuple] = tuple(f"{i * px + overscan:.3f}" for i in range(w + 1))
    else:
        XM = XP = None

    body = _l2l_native.generate(
        img.tobytes(),
        w,
        h,
        2 if has_alpha else 1,
        int(profile.invert),
        255 - profile.white_threshold,
        profile.s_min,
        profile.s_max,
        XS,
        YS,
        SS,
        int(profile.bidirectional),
        XM,
        XP,
    )
    return chain(_gcode_header(image_path, profile, w, h), body, ("M5", "G0 X0 Y0"))


_FIRMWARES = ("grbl", "smoothie", "marlin", "vigowork")
_COLOR_FILTERS = ("all", "red", "green", "blue", "black")


@dataclass(frozen=True)
class SvgProfile:
    """SVG vector-engraving calibration: one machine/material recipe for
    the svg_gcode algorithm. Frozen and validated, like L2LProfile.

    The defaults reproduce LaserGRBL's own SVG import defaults, so the
    generated G-code matches what the desktop app would emit for the
    same drawing.

    Attributes:
        feed: Cutting/tracing feed rate in mm/min (LaserGRBL's
            "BorderSpeed").
        s_max: Laser power (S) applied while the beam is down. 255 is
            LaserGRBL's PowerMax default.
        support_pwm: True emits S-word power control (S{s_max} down, S0
            up) — the GRBL diode-laser norm. False toggles the beam with
            the raw laser_on/laser_off commands instead.
        laser_on: "M3" (constant power) or "M4" (dynamic power).
        laser_off: Beam-off command, normally "M5".
        to_mm: Convert SVG user units to millimetres (True) or inches.
        smart_bezier: Adaptive curve flattening (error-bounded). False
            uses the legacy fixed bezier_accuracy segments per curve.
        bezier_accuracy: Segments per curve in legacy mode.
        firmware: "grbl", "smoothie", "marlin" or "vigowork". Only
            "smoothie" changes output (power scaled to 0-1 and repeated
            on G1/G2/G3).
        scale_to_max: Rescale the whole drawing so its largest dimension
            equals max_size_mm.
        max_size_mm: Target largest dimension when scale_to_max is set.
        color_filter: Engrave only strokes/fills of a given color:
            "all", "red", "green", "blue" or "black".
        reduce: Drop G1 moves shorter than reduce_value (point thinning).
        reduce_value: Minimum move length in mm when reduce is set.
        no_arcs: Emit arcs (circles, rounded rects) as G1 line segments
            instead of G2/G3.
        offset_x: X offset in mm added to every coordinate.
        offset_y: Y offset in mm added to every coordinate.

    Raises:
        TypeError: On construction, if a field has the wrong type.
        ValueError: On construction, if feed is not positive, s_max is
            negative, bezier_accuracy or max_size_mm are not positive,
            reduce_value is negative, laser_on is not "M3"/"M4", or
            firmware/color_filter are not recognized.
    """

    feed: int = 1000
    s_max: int = 255
    support_pwm: bool = True
    laser_on: str = "M3"
    laser_off: str = "M5"
    to_mm: bool = True
    smart_bezier: bool = True
    bezier_accuracy: int = 12
    firmware: str = "grbl"
    scale_to_max: bool = False
    max_size_mm: float = 100.0
    color_filter: str = "all"
    reduce: bool = False
    reduce_value: float = 0.1
    no_arcs: bool = False
    offset_x: float = 0.0
    offset_y: float = 0.0

    def __post_init__(self) -> None:
        for name in ("feed", "s_max", "bezier_accuracy"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
        for name in ("max_size_mm", "reduce_value", "offset_x", "offset_y"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number, got {type(value).__name__}")
        for name in ("support_pwm", "to_mm", "smart_bezier", "scale_to_max",
                     "reduce", "no_arcs"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")

        if self.feed <= 0:
            raise ValueError(f"feed must be positive, got {self.feed}")
        if self.s_max < 0:
            raise ValueError(f"s_max must be >= 0, got {self.s_max}")
        if self.bezier_accuracy <= 0:
            raise ValueError(
                f"bezier_accuracy must be positive, got {self.bezier_accuracy}"
            )
        if self.max_size_mm <= 0:
            raise ValueError(f"max_size_mm must be positive, got {self.max_size_mm}")
        if self.reduce_value < 0:
            raise ValueError(f"reduce_value must be >= 0, got {self.reduce_value}")
        if self.laser_on not in ("M3", "M4"):
            raise ValueError(f"laser_on must be 'M3' or 'M4', got {self.laser_on!r}")
        if self.firmware not in _FIRMWARES:
            raise ValueError(
                f"firmware must be one of {_FIRMWARES}, got {self.firmware!r}"
            )
        if self.color_filter not in _COLOR_FILTERS:
            raise ValueError(
                f"color_filter must be one of {_COLOR_FILTERS}, "
                f"got {self.color_filter!r}"
            )


def _svg_header(svg_path: str, profile: SvgProfile) -> list:
    """Traceability/setup preamble: library version, source hash, the
    full profile and the modal setup (absolute, mm, beam off)."""
    return [
        f"; pygrbl_build v{__version__}",
        f"; svg: {Path(svg_path).name} sha256:{_file_sha256(svg_path)}",
        f"; profile: {profile}",
        "G90",
        "G21",
        f"{profile.laser_on} S0",
    ]


def svg_gcode(svg_path: str, profile: SvgProfile) -> Iterator[str]:
    """Generate vector G-code from an SVG, line by line.

    Faithful port of LaserGRBL's SVG import: parses paths, basic shapes
    (rect, circle, ellipse, line, polyline, polygon) and nested groups,
    applies their transforms, flattens curves to segments and emits the
    same G-code the desktop app would, including its single-letter move
    comments and modal G-code compression. text/image elements are
    skipped (convert text to paths in your editor first).

    Output: absolute coordinates (G90), millimetres (G21, unless
    to_mm=False), Y flipped so the drawing grows upward, scaled by the
    SVG's width/height/viewBox exactly as LaserGRBL computes it.

    Args:
        svg_path: Path to an .svg file.
        profile: The calibration to engrave with.

    Returns:
        Iterator of G-code lines, without trailing newlines.

    Raises:
        FileNotFoundError: If svg_path does not exist.
        xml.etree.ElementTree.ParseError: If the file is not valid XML.
    """
    body = _svg.convert(svg_path, profile)
    return chain(_svg_header(svg_path, profile), body, ("M5 S0", "G0 X0 Y0"))


_TURNPOLICIES = ("minority", "majority", "right", "black", "white")
_IMG_FORMULAS = (
    "simple_average", "weight_average", "optical_correct", "custom"
)


@dataclass(frozen=True)
class Img2VectorProfile:
    """Image vector-tracing calibration: one machine/material recipe for
    the img2vector_gcode algorithm. Frozen and validated, like the others.

    This is LaserGRBL's "Vectorize!" mode: the image is reduced to black
    and white, Potrace traces its outlines as closed contours of lines and
    cubic Beziers, and each Bezier is emitted as G2/G3 arcs (or a G1
    fallback). v1 traces outlines only — no interior filling.

    The defaults follow Potrace's classic settings (smooth curves,
    optimization on), which give the best trace out of the box. LaserGRBL's
    own UI defaults differ (smoothing/optimize off → alphamax=0.0,
    opticurve=False); set those explicitly to mimic the desktop app.

    Attributes:
        width_mm: Physical width of the engraving in mm. Height follows the
            image's aspect ratio.
        quality: Tracing resolution in pixels per mm (LaserGRBL's vector
            "Quality"). The bitmap is width_mm*quality px wide; coordinates
            are divided by this to get millimetres. 10 is the desktop
            default.
        feed: Tracing feed rate in mm/min (LaserGRBL's BorderSpeed).
        s_max: Laser power (S) while the beam is down. With support_pwm the
            beam toggles S{s_max}/S0.
        support_pwm: True emits S-word power control (S{s_max} down, S0 up),
            the GRBL diode-laser norm. False toggles with laser_on/laser_off.
        laser_on: "M3" (constant power) or "M4" (dynamic power).
        laser_off: Beam-off command, normally "M5".
        turdsize: Despeckle. Contours with area <= turdsize are dropped.
        turnpolicy: Diagonal-ambiguity rule: "minority", "majority",
            "right", "black" or "white". Potrace's default is "minority".
        alphamax: Corner threshold (0.0-1.334). Higher = rounder corners;
            0.0 makes every vertex a sharp corner.
        opttolerance: Curve-optimization tolerance. Higher = more aggressive
            merging of Beziers.
        opticurve: Run the curve-optimization stage (Potrace's optiCurve).
        formula: Grayscale weights: "simple_average", "weight_average",
            "optical_correct" or "custom". Grayscale inputs are forced to
            "simple_average", exactly like LaserGRBL.
        red, green, blue: Per-channel weights (0-100), only used by "custom".
        brightness: 0-100. 100 = unchanged; lower darkens.
        contrast: 0-100+. Linear channel scale (100 = unchanged).
        white_clip: Near-white clip (0-100). Pixels within this of pure
            white are dropped (treated as background). 0 disables it.
        threshold: Binarization cut 0-100, only applied when use_threshold.
        use_threshold: Apply the threshold cut before tracing. When False
            the gray image still gets binarized by Potrace at R+G+B<382.5.
        offset_x: X offset in mm added to every coordinate.
        offset_y: Y offset in mm added to every coordinate.

    Raises:
        TypeError: On construction, if a field has the wrong type.
        ValueError: On construction, if a numeric field is out of range,
            laser_on is not "M3"/"M4", or turnpolicy/formula are not
            recognized.
    """

    width_mm: float
    quality: float = 10.0
    feed: int = 1000
    s_max: int = 1000
    support_pwm: bool = True
    laser_on: str = "M3"
    laser_off: str = "M5"
    turdsize: int = 2
    turnpolicy: str = "minority"
    alphamax: float = 1.0
    opttolerance: float = 0.2
    opticurve: bool = True
    formula: str = "simple_average"
    red: int = 100
    green: int = 100
    blue: int = 100
    brightness: int = 100
    contrast: int = 100
    white_clip: int = 5
    threshold: int = 50
    use_threshold: bool = False
    offset_x: float = 0.0
    offset_y: float = 0.0

    def __post_init__(self) -> None:
        for name in ("width_mm", "quality", "alphamax", "opttolerance",
                     "offset_x", "offset_y"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number, got {type(value).__name__}")
        for name in ("feed", "s_max", "turdsize", "red", "green", "blue",
                     "brightness", "contrast", "white_clip", "threshold"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
        for name in ("support_pwm", "opticurve", "use_threshold"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")

        if self.width_mm <= 0:
            raise ValueError(f"width_mm must be positive, got {self.width_mm}")
        if self.quality <= 0:
            raise ValueError(f"quality must be positive, got {self.quality}")
        if self.feed <= 0:
            raise ValueError(f"feed must be positive, got {self.feed}")
        if self.s_max < 0:
            raise ValueError(f"s_max must be >= 0, got {self.s_max}")
        if self.turdsize < 0:
            raise ValueError(f"turdsize must be >= 0, got {self.turdsize}")
        if self.alphamax < 0:
            raise ValueError(f"alphamax must be >= 0, got {self.alphamax}")
        if self.opttolerance < 0:
            raise ValueError(f"opttolerance must be >= 0, got {self.opttolerance}")
        for name in ("red", "green", "blue", "brightness", "white_clip",
                     "threshold"):
            value = getattr(self, name)
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be in 0-100, got {value}")
        if self.contrast < 0:
            raise ValueError(f"contrast must be >= 0, got {self.contrast}")
        if self.laser_on not in ("M3", "M4"):
            raise ValueError(f"laser_on must be 'M3' or 'M4', got {self.laser_on!r}")
        if self.turnpolicy not in _TURNPOLICIES:
            raise ValueError(
                f"turnpolicy must be one of {_TURNPOLICIES}, got {self.turnpolicy!r}"
            )
        if self.formula not in _IMG_FORMULAS:
            raise ValueError(
                f"formula must be one of {_IMG_FORMULAS}, got {self.formula!r}"
            )


def _img2vec_header(image_path: str, profile: Img2VectorProfile) -> list:
    """Traceability/setup preamble: library version, source hash, the full
    profile and the modal setup (absolute, mm, beam off, feed)."""
    return [
        f"; pygrbl_build v{__version__}",
        f"; image: {Path(image_path).name} sha256:{_file_sha256(image_path)}",
        f"; profile: {profile}",
        "G90",
        "G21",
        f"{profile.laser_on} S0",
        f"G1 F{profile.feed}",
    ]


def img2vector_gcode(image_path: str, profile: Img2VectorProfile) -> Iterator[str]:
    """Generate vector G-code by tracing an image's outlines, line by line.

    Faithful port of LaserGRBL's "Vectorize!": the image is reduced to
    black/white (resize, grayscale, white-clip, optional threshold),
    Potrace traces its outlines as closed contours, and each cubic Bezier
    is approximated by biarcs and emitted as G2/G3 arcs (with a G1
    fallback). v1 traces outlines only (no interior filling).

    Output: absolute coordinates (G90), millimetres (G21), Y flipped so the
    drawing grows upward, scaled so width equals width_mm.

    Args:
        image_path: Path to an image (any Pillow-readable format). Color is
            reduced to gray with the profile's formula.
        profile: The calibration to engrave with.

    Returns:
        Iterator of G-code lines, without trailing newlines.

    Raises:
        FileNotFoundError: If image_path does not exist.
    """
    body = _img2vec.convert(image_path, profile)
    return chain(_img2vec_header(image_path, profile), body, ("M5 S0", "G0 X0 Y0"))


def write_gcode(
    lines: Iterable[str],
    output_path: str,
    chunk: int = 200_000,
) -> int:
    """Write G-code lines to a plain text file, batched for speed.

    The path is written verbatim — pick whatever extension you want
    (.nc, .gcode, .g, ...); this helper never renames or appends one.
    Compression, if needed, is the upper layer's job: pass the lines
    to whatever sink you want instead of using this helper.

    Args:
        lines: Iterable of G-code lines (e.g. from l2l_gcode).
        output_path: Destination file path, written verbatim.
        chunk: Lines per write.

    Returns:
        Number of lines written.
    """
    n = 0
    buf = []
    with open(output_path, "w") as f:
        for line in lines:
            buf.append(line)
            if len(buf) >= chunk:
                f.write("\n".join(buf))
                f.write("\n")
                n += len(buf)
                buf.clear()
        if buf:
            f.write("\n".join(buf))
            f.write("\n")
            n += len(buf)
    return n
