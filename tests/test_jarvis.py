from io import BytesIO

from PIL import Image
import pytest

from pygrbl_build import JarvisProfile, jarvis_gcode
from pygrbl_build import _l2l_native
from pygrbl_build import _jarvis


def test_jarvis_reference_kernel_and_edge_behavior():
    # LaserGRBL/Cyotek: first row has no same-row diffusion; targets in
    # column zero never receive error. The later rows use the 48 divisor.
    result = _l2l_native.dither_jarvis(bytes([100] * 20), 5, 4)
    assert list(result) == [
        0, 0, 0, 0, 0,
        0, 255, 255, 0, 255,
        0, 255, 0, 0, 255,
        0, 0, 0, 255, 0,
    ]
    assert _l2l_native.dither_jarvis(bytes([127] * 4), 4, 1) == bytes(4)
    # LaserGRBL's second luminance conversion truncates gray 128 to 127.
    assert _l2l_native.dither_jarvis(bytes([128]), 1, 1) == b"\x00"


def test_jarvis_raster_emits_binary_power_and_skips_white_rows():
    image = Image.new("L", (4, 2), 255)
    image.putpixel((1, 1), 0)
    commands = list(jarvis_gcode(image, JarvisProfile(width_mm=4, lines_per_mm=1,
                                                      s_max=250)))
    assert commands[8:-2] == ["G0 X1.000 Y0.000 S0", "G1 X2.000 S250"]
    assert commands[-2:] == ["M5", "G0 X0 Y0"]


def test_jarvis_sources_and_transparency(tmp_path):
    image = Image.new("RGBA", (3, 1), (0, 0, 0, 255))
    image.putpixel((1, 0), (0, 0, 0, 0))
    source = BytesIO()
    image.save(source, format="PNG")
    content = source.getvalue()
    path = tmp_path / "dots.png"
    path.write_bytes(content)
    profile = JarvisProfile(width_mm=3, lines_per_mm=1)

    from_path = list(jarvis_gcode(path, profile))
    from_bytes = list(jarvis_gcode(content, profile))
    from_bytearray = list(jarvis_gcode(bytearray(content), profile))
    from_image = list(jarvis_gcode(image, profile))
    assert from_path[2:] == from_bytes[2:] == from_bytearray[2:] == from_image[2:]
    assert from_bytes[8:-2] == [
        "G0 X0.000 Y0.000 S0", "G1 X1.000 S1000",
        "X2.000 S0", "X3.000 S1000",
    ]


def test_jarvis_preprocessing_honors_color_and_white_clip():
    image = Image.new("RGB", (2, 1), "white")
    image.putpixel((0, 0), (255, 0, 0))
    profile = JarvisProfile(width_mm=2, lines_per_mm=1, formula="optical_correct")
    dots = _jarvis.prepare(image, profile)
    assert list(dots.getchannel("L").getdata()) == [0, 255]
    assert list(dots.getchannel("A").getdata()) == [255, 0]


@pytest.mark.parametrize("field,value", [
    ("width_mm", float("nan")), ("lines_per_mm", float("inf")),
    ("feed", 0), ("s_max", 0), ("white_clip", 101),
    ("formula", "unknown"), ("bidirectional", 1),
])
def test_jarvis_profile_rejects_invalid_settings(field, value):
    settings = {"width_mm": 1, field: value}
    with pytest.raises((TypeError, ValueError)):
        JarvisProfile(**settings)
