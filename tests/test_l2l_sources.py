from io import BytesIO

from PIL import Image
import pytest

from pygrbl_build import L2LProfile, l2l_gcode


def png() -> bytes:
    image = Image.new("L", (4, 3), 255)
    image.putpixel((1, 1), 0)
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_l2l_accepts_path_bytes_bytearray_and_pillow(tmp_path):
    content = png()
    path = tmp_path / "source.png"
    path.write_bytes(content)
    profile = L2LProfile(width_mm=4, lines_per_mm=1)

    from_path = list(l2l_gcode(path, profile))
    from_bytes = list(l2l_gcode(content, profile))
    from_bytearray = list(l2l_gcode(bytearray(content), profile))
    with Image.open(BytesIO(content)) as image:
        from_pillow = list(l2l_gcode(image, profile))

    assert from_path[2:] == from_bytes[2:] == from_bytearray[2:] == from_pillow[2:]
    assert "image: source.png sha256:" in from_path[1]
    assert "image: <memory> sha256:" in from_bytes[1]
    assert from_bytes[1] == from_bytearray[1]
    assert "image: <Pillow image> sha256:" in from_pillow[1]


def test_l2l_rejects_unsupported_source():
    with pytest.raises(TypeError, match="image source"):
        l2l_gcode(object(), L2LProfile(width_mm=1))
