"""Extension build hook for pygrbl_build's C engine.

All package metadata lives in pyproject.toml; this file exists only
because declarative config cannot describe ext_modules.
"""

from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "pygrbl_build._l2l_native",
            sources=["src/pygrbl_build/_l2l_native.c"],
        ),
        Extension(
            "pygrbl_build._gcode_parser",
            sources=[
                "src/pygrbl_build/_gcode_parser.c",
                "src/pygrbl_build/gcode_parser.c",
            ],
            include_dirs=["src/pygrbl_build"],
        ),
    ],
)
