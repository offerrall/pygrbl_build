# Changelog

All notable changes to this project are documented in this file.

## 0.3.0 - 2026-06-19

### Added

- **G-code bounds & framing** (`get_bounding_box` + `generate_framing_gcode`):
  the [gcode-bounds](https://github.com/offerrall/gcode-bounds) library
  folded in as a second C extension (`_gcode_parser`), same C parser as
  the original (`fast_atof` + line scan, 500MB+ files in seconds).
  `get_bounding_box` computes the `(min_x, max_x, min_y, max_y)` extent of
  some G-code; `generate_framing_gcode` traces that box (the framing pass)
  so the operator can confirm placement before engraving. Rapid moves to
  the origin (`G0` with `X0`/`Y0`) are skipped so the library's own home
  moves don't expand the box.
- `get_bounding_box` accepts the G-code as a file path (`str`/`Path`,
  opened and streamed in C) **or** as raw content already in memory
  (`bytes`/`bytearray`, or a multi-line `str`), so it no longer has to
  exist on disk. The Python wrapper decides which route to take; a new C
  entry point (`get_bounding_box_buffer`) parses the in-memory buffer with
  the same per-line logic as the file path.

## 0.2.0 - 2026-06-16

### Added

- **Image to SVG** algorithm (`img2svg` + `Img2SvgProfile`): traces a
  raster image to a standard vector SVG, reusing the same Potrace core
  and image preprocessing as `img2vector_gcode` but skipping the
  biarc/G-code stages. The contours are written as a single filled
  `<path>` with `fill-rule="evenodd"`, so inner contours become holes
  (the filled black silhouette potrace.exe produces). `viewBox` is in
  pixels while `width`/`height` carry the physical size in mm, and the
  bitmap is not Y-flipped, keeping the image's natural top-down
  orientation. `Img2SvgProfile` carries only the tracing and
  binarization knobs (no feed/power/laser-mode fields). Pure Python,
  Pillow only.

## 0.1.0 - 2026-06-16

### Added

- **Image vector** algorithm (`img2vector_gcode` + `Img2VectorProfile`):
  outline tracing from a raster image, a faithful Python port of
  LaserGRBL's "Vectorize!" mode. The image is reduced to black/white
  (resize, grayscale, white-clip, optional threshold), Potrace traces its
  outlines as closed contours, and each cubic Bezier is approximated by
  biarcs and emitted as `G2`/`G3` arcs (with a `G1` fallback). Pure
  Python, Pillow only. Outlines only — interior filling is not ported yet.

