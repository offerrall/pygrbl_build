# Limitations

## Raster

- Line-to-Line accepts grayscale images only; color conversion is left to an
  image editor.
- Jarvis engraves horizontal raster passes only; vertical and diagonal
  directions are not implemented.
- Jarvis's diffusion kernel follows LaserGRBL, but image resizing uses Pillow,
  so byte-for-byte parity with LaserGRBL is not guaranteed for Jarvis.

## Vector

- `svg_gcode` skips `text` and `image` elements: convert text to paths in an
  editor first.
- `img2vector_gcode` traces outlines only; it does not fill interiors.

## Platforms

Prebuilt wheels exist for Linux x86_64 (glibc) and Windows x86-64. On other
platforms, such as macOS, ARM boards like the Raspberry Pi, or musl-based
Linux, installation builds the C extensions from the source distribution and
needs a C compiler.
