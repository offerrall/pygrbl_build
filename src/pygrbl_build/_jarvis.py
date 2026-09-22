"""Image preparation for LaserGRBL's Jarvis raster mode."""

from PIL import Image

from . import _l2l_native


_FORMULA_WEIGHTS = {
    "simple_average": (0.333, 0.333, 0.333),
    "weight_average": (0.333, 0.444, 0.222),
    "optical_correct": (0.299, 0.587, 0.114),
}


def prepare(image: Image.Image, profile) -> Image.Image:
    """Resize, adjust gray/alpha, white-clip, then dither in C.

    The diffusion runs on grayscale even for transparent pixels, as in
    LaserGRBL. Alpha is kept only for the final G-code power mapping.
    """
    image = image.convert("RGBA")
    width = max(1, round(profile.width_mm * profile.lines_per_mm))
    height = max(1, round(width * image.height / image.width))
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.BICUBIC)

    formula = profile.formula
    if formula != "custom" and _visually_gray(image):
        formula = "simple_average"
    if formula == "custom":
        weights = tuple(0.333 * v / 100 for v in
                        (profile.red, profile.green, profile.blue))
    else:
        weights = _FORMULA_WEIGHTS[formula]
    contrast = profile.contrast / 100
    matrix = tuple(v * contrast for v in weights) + (
        -(100 - profile.brightness) * 255 / 100,
    )
    gray = image.convert("RGB").convert("L", matrix)
    alpha = image.getchannel("A")
    if profile.white_clip:
        clip = gray.point(lambda value: 0 if value > 255 - profile.white_clip else 255)
        alpha = Image.composite(alpha, Image.new("L", gray.size), clip)

    binary = Image.frombytes("L", gray.size,
                             _l2l_native.dither_jarvis(gray.tobytes(), width, height))
    return Image.merge("LA", (binary, alpha))


def _visually_gray(image: Image.Image) -> bool:
    pixels = image.convert("RGB").load()
    for y in range(0, image.height, 10):
        for x in range(0, image.width, 10):
            color = pixels[x, y]
            if max(color) - min(color) >= 20:
                return False
    return True
