from io import BytesIO

from PIL import Image
import pytest

from pygrbl_build import (
    Img2SvgProfile,
    Img2VectorProfile,
    L2LProfile,
    SvgProfile,
    img2svg,
    img2vector_gcode,
    l2l_gcode,
    svg_gcode,
)


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


def test_l2l_omits_only_redundant_modal_g1_words():
    image = Image.new("L", (6, 2), 255)
    image.putdata((0, 64, 128, 192, 64, 0) * 2)
    commands = list(l2l_gcode(image, L2LProfile(width_mm=6, lines_per_mm=1)))
    body = commands[8:-2]

    rapid_rows = linear_starts = compact_linear_moves = 0
    motion = None
    for command in body:
        if command.startswith("G0"):
            motion = "G0"
            rapid_rows += 1
        elif command.startswith("G1"):
            motion = "G1"
            linear_starts += 1
        elif command.startswith("X"):
            assert motion == "G1"
            compact_linear_moves += 1

    assert rapid_rows == linear_starts == 2
    assert compact_linear_moves > 0


def test_image_vector_apis_accept_path_bytes_bytearray_and_pillow(tmp_path):
    content = png()
    path = tmp_path / "source.png"
    path.write_bytes(content)
    gcode_profile = Img2VectorProfile(width_mm=4, quality=1)
    svg_profile = Img2SvgProfile(width_mm=4, quality=1)

    from_path = list(img2vector_gcode(path, gcode_profile))
    from_bytes = list(img2vector_gcode(content, gcode_profile))
    from_bytearray = list(img2vector_gcode(bytearray(content), gcode_profile))
    with Image.open(BytesIO(content)) as image:
        from_pillow = list(img2vector_gcode(image, gcode_profile))
        svg_from_pillow = img2svg(image, svg_profile)

    assert from_path[2:] == from_bytes[2:] == from_bytearray[2:] == from_pillow[2:]
    assert img2svg(path, svg_profile) == img2svg(content, svg_profile)
    assert img2svg(bytearray(content), svg_profile) == svg_from_pillow


def test_svg_gcode_accepts_path_text_bytes_and_bytearray(tmp_path):
    content = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="3"><rect x="1" y="1" width="2" height="1"/></svg>'
    path = tmp_path / "shape.svg"
    path.write_bytes(content)
    profile = SvgProfile()

    from_path = list(svg_gcode(path, profile))
    from_text = list(svg_gcode(content.decode(), profile))
    from_bytes = list(svg_gcode(content, profile))
    from_bytearray = list(svg_gcode(bytearray(content), profile))

    assert from_path[2:] == from_text[2:] == from_bytes[2:] == from_bytearray[2:]
    assert "svg: shape.svg sha256:" in from_path[1]
    assert "svg: <memory> sha256:" in from_text[1]
    assert from_text[1] == from_bytes[1] == from_bytearray[1]


def test_other_apis_reject_unsupported_sources():
    with pytest.raises(TypeError, match="image source"):
        img2vector_gcode(object(), Img2VectorProfile(width_mm=1))
    with pytest.raises(TypeError, match="image source"):
        img2svg(object(), Img2SvgProfile(width_mm=1))
    with pytest.raises(TypeError, match="SVG source"):
        svg_gcode(object(), SvgProfile())
