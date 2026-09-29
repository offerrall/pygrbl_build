# Releasing

[Back to README](README.md)

`.github/workflows/build.yml` builds and tests on pushes to `main` (excluding
documentation-only changes), on published GitHub releases, and on manual runs.
Publishing a GitHub release automatically publishes its distributions to PyPI
after all required jobs succeed. Pushes and manual runs only create CI artifacts.

Before creating a release, update `__version__` in `src/pygrbl_build/__init__.py`
and use the matching version for the release tag (for example, `v1.0.0`).
The workflow checks this match; it does not increment versions or skip existing
PyPI files.

One-time setup: create the `pypi-release` GitHub environment and add a GitHub
Trusted Publisher in the PyPI project's Publishing settings with:

- Owner: `offerrall`
- Repository: `pygrbl_build`
- Workflow filename: `build.yml`
- Environment: `pypi-release`

Authentication uses OIDC (`id-token: write`); no PyPI API token secret is needed.
See [PyPI's Trusted Publisher setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).
