# Changelog

All notable changes to this project are documented in this file.

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

