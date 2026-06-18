#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <float.h>

#define GCODE_SUCCESS 0
#define GCODE_ERROR_FILE_NOT_FOUND -1
#define GCODE_ERROR_NO_COORDINATES -2

static inline double fast_atof(const char* str) {
    double result = 0.0;
    double sign = 1.0;
    double fraction = 0.0;
    double divisor = 1.0;
    int in_fraction = 0;

    if (*str == '-') {
        sign = -1.0;
        str++;
    } else if (*str == '+') {
        str++;
    }

    while (*str && (*str >= '0' && *str <= '9' || *str == '.')) {
        if (*str == '.' && !in_fraction) {
            in_fraction = 1;
        } else if (*str >= '0' && *str <= '9') {
            if (in_fraction) {
                divisor *= 10.0;
                fraction = fraction * 10.0 + (*str - '0');
            } else {
                result = result * 10.0 + (*str - '0');
            }
        }
        str++;
    }

    return sign * (result + fraction / divisor);
}

static void update_bbox_from_line(const char* line,
                                  double* min_x,
                                  double* max_x,
                                  double* min_y,
                                  double* max_y) {

    const char* p = line;

    while (*p && *p != 'X' && *p != 'Y') {
        p++;
    }

    while (*p) {
        if (*p == 'X' || *p == 'Y') {
            char axis = *p++;

            // Skip spaces
            while (*p == ' ' || *p == '\t') {
                p++;
            }

            // Fast number parsing
            double val = fast_atof(p);

            // Update bounding box
            if (axis == 'X') {
                if (val < *min_x) *min_x = val;
                if (val > *max_x) *max_x = val;
            } else {
                if (val < *min_y) *min_y = val;
                if (val > *max_y) *max_y = val;
            }

            // Skip past number
            while (*p >= '0' && *p <= '9' || *p == '.' || *p == '-' || *p == '+') {
                p++;
            }
        } else {
            p++;
        }
    }
}

static int get_bounding_box(const char* file_path,
                            double* min_x,
                            double* max_x,
                            double* min_y,
                            double* max_y) {

    *min_x = DBL_MAX;
    *max_x = -DBL_MAX;
    *min_y = DBL_MAX;
    *max_y = -DBL_MAX;

    FILE* file = fopen(file_path, "r");
    if (!file) {
        return GCODE_ERROR_FILE_NOT_FOUND;
    }

    char line[1024];
    while (fgets(line, sizeof(line), file)) {

        if (line[0] == 'G' && line[1] == '0' &&
            strstr(line, "X0") && strstr(line, "Y0")) {
            continue;
        }

        update_bbox_from_line(line, min_x, max_x, min_y, max_y);
    }

    fclose(file);

    if (*min_x == DBL_MAX || *min_y == DBL_MAX) {
        return GCODE_ERROR_NO_COORDINATES;
    }

    return GCODE_SUCCESS;
}

static int get_bounding_box_buffer(const char* data,
                                   long len,
                                   double* min_x,
                                   double* max_x,
                                   double* min_y,
                                   double* max_y) {

    *min_x = DBL_MAX;
    *max_x = -DBL_MAX;
    *min_y = DBL_MAX;
    *max_y = -DBL_MAX;

    // Same per-line logic as get_bounding_box, but over an in-memory
    // buffer instead of a file. Lines are copied into the same 1024-byte
    // scratch as fgets uses, so behaviour matches the file path exactly.
    char line[1024];
    long i = 0;
    while (i < len) {
        long j = 0;
        while (i < len && data[i] != '\n' && j < (long)sizeof(line) - 1) {
            line[j++] = data[i++];
        }
        line[j] = '\0';
        // Drop the rest of an over-long physical line, then the newline.
        while (i < len && data[i] != '\n') i++;
        if (i < len) i++;

        if (line[0] == 'G' && line[1] == '0' &&
            strstr(line, "X0") && strstr(line, "Y0")) {
            continue;
        }

        update_bbox_from_line(line, min_x, max_x, min_y, max_y);
    }

    if (*min_x == DBL_MAX || *min_y == DBL_MAX) {
        return GCODE_ERROR_NO_COORDINATES;
    }

    return GCODE_SUCCESS;
}

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
