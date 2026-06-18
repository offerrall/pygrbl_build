#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include "gcode_parser.h"

static PyObject* py_get_bounding_box(PyObject* self, PyObject* args) {
    const char* file_path;

    if (!PyArg_ParseTuple(args, "s", &file_path)) {
        return NULL;
    }

    double min_x, max_x, min_y, max_y;
    int result = get_bounding_box(file_path, &min_x, &max_x, &min_y, &max_y);

    switch (result) {
        case GCODE_ERROR_FILE_NOT_FOUND:
            PyErr_SetString(PyExc_FileNotFoundError, "Could not open G-code file");
            return NULL;

        case GCODE_ERROR_NO_COORDINATES:
            PyErr_SetString(PyExc_ValueError, "No valid X/Y coordinates found in file");
            return NULL;

        case GCODE_SUCCESS:
            return Py_BuildValue("(dddd)",
                                 min_x,
                                 max_x,
                                 min_y,
                                 max_y);

        default:
            PyErr_SetString(PyExc_RuntimeError, "Unknown error occurred");
            return NULL;
    }
}

static PyObject* py_get_bounding_box_buffer(PyObject* self, PyObject* args) {
    Py_buffer view;

    if (!PyArg_ParseTuple(args, "y*", &view)) {
        return NULL;
    }

    double min_x, max_x, min_y, max_y;
    int result = get_bounding_box_buffer((const char*)view.buf, (long)view.len,
                                         &min_x, &max_x, &min_y, &max_y);
    PyBuffer_Release(&view);

    switch (result) {
        case GCODE_ERROR_NO_COORDINATES:
            PyErr_SetString(PyExc_ValueError, "No valid X/Y coordinates found in G-code");
            return NULL;

        case GCODE_SUCCESS:
            return Py_BuildValue("(dddd)",
                                 min_x,
                                 max_x,
                                 min_y,
                                 max_y);

        default:
            PyErr_SetString(PyExc_RuntimeError, "Unknown error occurred");
            return NULL;
    }
}

static PyMethodDef gcode_parser_methods[] = {
    {"get_bounding_box", py_get_bounding_box, METH_VARARGS, "Extract bounding box coordinates from G-code file"},
    {"get_bounding_box_buffer", py_get_bounding_box_buffer, METH_VARARGS, "Extract bounding box coordinates from in-memory G-code bytes"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef gcode_parser_module = {PyModuleDef_HEAD_INIT,
                                                 "_gcode_parser",
                                                 "Fast C implementation for parsing G-code files",
                                                 -1,
                                                 gcode_parser_methods
};

PyMODINIT_FUNC PyInit__gcode_parser(void) {
    return PyModule_Create(&gcode_parser_module);
}
