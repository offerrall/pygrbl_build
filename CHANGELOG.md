# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Added

- **Image vector** algorithm (`img2vector_gcode` + `Img2VectorProfile`):
  outline tracing from a raster image, a faithful Python port of
  LaserGRBL's "Vectorize!" mode. The image is reduced to black/white
  (resize, grayscale, white-clip, optional threshold), Potrace traces its
  outlines as closed contours, and each cubic Bezier is approximated by
  biarcs and emitted as `G2`/`G3` arcs (with a `G1` fallback). Pure
  Python, Pillow only. Outlines only — interior filling is not ported yet.

