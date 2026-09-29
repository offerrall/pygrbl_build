# Output

`write_gcode` writes the path verbatim, so you choose the extension
(`.nc`, `.gcode`, `.g`, ...). It's just a convenience: every `*_gcode`
generator is a lazy iterator of lines, so anything beyond writing a
plain file (compression, network shipping, streaming to the machine) is
the upper layer's job — consume the iterator with whatever sink you need.

Public API: `L2LProfile`, `l2l_gcode`, `JarvisProfile`, `jarvis_gcode`,
`SvgProfile`, `svg_gcode`,
`Img2VectorProfile`, `img2vector_gcode`, `Img2SvgProfile`, `img2svg`,
`get_bounding_box`, `generate_framing_gcode`, `write_gcode`. See the
docstrings.
