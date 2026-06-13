#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

typedef struct {
    PyObject_HEAD
    /* owned references that keep every borrowed pointer below alive;
     * the tables are tuples, so their items cannot be swapped out
     * under the cached UTF-8 pointers */
    PyObject *pixels;            /* bytes: post-resize raw pixel data   */
    PyObject *tables[5];         /* xs, ys, ss, xm, xp str-table tuples */

    const unsigned char *pix;
    int w, h, channels;          /* channels: 1 = L, 2 = LA            */
    int bidirectional, overscan; /* flags                              */
    int ltr;                     /* current scan direction             */
    int row;                     /* next row to examine, h-1 .. -1     */

    int *map;                    /* gray([,alpha]) -> power, 256/64K   */

    /* string tables: borrowed UTF-8 pointers into the kept-alive strs */
    const char **xs, **ys, **ss, **xm, **xp;
    int *xs_n, *ys_n, *ss_n, *xm_n, *xp_n;

    /* per-row scratch */
    int *run_start, *run_pw;     /* RLE: run starts and run powers     */
    char *buf;                   /* assembled text of the current row  */
    size_t *off;                 /* off[i] = start of line i in buf;
                                    off[line_count] = end of last line */
    int line_count, line_pos;
} L2LIter;

/* ------------------------------------------------------------------ */

static void
L2L_dealloc(L2LIter *it)
{
    PyMem_Free(it->map);
    PyMem_Free((void *)it->xs); PyMem_Free(it->xs_n);
    PyMem_Free((void *)it->ys); PyMem_Free(it->ys_n);
    PyMem_Free((void *)it->ss); PyMem_Free(it->ss_n);
    PyMem_Free((void *)it->xm); PyMem_Free(it->xm_n);
    PyMem_Free((void *)it->xp); PyMem_Free(it->xp_n);
    PyMem_Free(it->run_start);
    PyMem_Free(it->run_pw);
    PyMem_Free(it->buf);
    PyMem_Free(it->off);
    Py_XDECREF(it->pixels);
    for (int i = 0; i < 5; i++)
        Py_XDECREF(it->tables[i]);
    Py_TYPE(it)->tp_free((PyObject *)it);
}

/* Replicates the reference pipeline per pixel value:
 *   rv = 255 - gray            (or (255-gray)*alpha/255 with LA)
 *   if invert: rv = 255 - rv
 *   if rv <= clip_max: rv = 0          (clip_max = 255 - white_threshold)
 *   power = 0 if rv == 0 else rv*(s_max-s_min)/255 + s_min
 * All values non-negative -> truncating '/' == Python '//' == C# '/'.
 */
static int *
build_power_map(int channels, int invert, int clip_max,
                int64_t smin, int64_t srange)
{
    int n = (channels == 2) ? 65536 : 256;
    int *m = PyMem_Malloc((size_t)n * sizeof(int));
    if (!m) {
        PyErr_NoMemory();
        return NULL;
    }
    for (int g = 0; g < 256; g++) {
        if (channels == 2) {
            for (int a = 0; a < 256; a++) {
                int rv = (255 - g) * a / 255;
                if (invert) rv = 255 - rv;
                if (rv <= clip_max) rv = 0;
                m[(g << 8) | a] =
                    rv ? (int)((int64_t)rv * srange / 255 + smin) : 0;
            }
        }
        else {
            int rv = 255 - g;
            if (invert) rv = 255 - rv;
            if (rv <= clip_max) rv = 0;
            m[g] = rv ? (int)((int64_t)rv * srange / 255 + smin) : 0;
        }
    }
    return m;
}

/* Cache the UTF-8 pointer/length of the first `need` strings of a
 * table tuple. The pointers stay valid because the iterator owns a
 * reference to the tuple, and a tuple's items cannot be replaced —
 * the strings live exactly as long as the iterator does. */
static int
build_table(PyObject *tup, Py_ssize_t need, const char *name,
            const char ***ptrs_out, int **lens_out, int *maxlen_io)
{
    if (PyTuple_GET_SIZE(tup) < need) {
        PyErr_Format(PyExc_ValueError,
                     "%s table needs %zd entries, got %zd",
                     name, need, PyTuple_GET_SIZE(tup));
        return -1;
    }
    const char **ptrs = PyMem_Malloc((size_t)need * sizeof(*ptrs));
    int *lens = PyMem_Malloc((size_t)need * sizeof(*lens));
    if (!ptrs || !lens) {
        PyMem_Free((void *)ptrs);
        PyMem_Free(lens);
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t i = 0; i < need; i++) {
        PyObject *item = PyTuple_GET_ITEM(tup, i);
        if (!PyUnicode_Check(item)) {
            PyErr_Format(PyExc_TypeError, "%s[%zd] is not a str", name, i);
            PyMem_Free((void *)ptrs);
            PyMem_Free(lens);
            return -1;
        }
        Py_ssize_t sl;
        const char *sp = PyUnicode_AsUTF8AndSize(item, &sl);
        if (!sp) {
            PyMem_Free((void *)ptrs);
            PyMem_Free(lens);
            return -1;
        }
        ptrs[i] = sp;
        lens[i] = (int)sl;
        if ((int)sl > *maxlen_io)
            *maxlen_io = (int)sl;
    }
    *ptrs_out = ptrs;
    *lens_out = lens;
    return 0;
}

/* Compute powers + RLE for `row`, and if it has any ink, assemble all
 * its G-code lines into it->buf. Returns 1 if lines were emitted, 0 if
 * the row is blank (reference: `if not powers.any(): continue`). */
static int
fill_row(L2LIter *it, int row)
{
    const int w = it->w;
    int *rs = it->run_start, *rp = it->run_pw;
    const int *map = it->map;
    int n, any, prev;

    /* power mapping + RLE in one pass; `any` ORs the run heads, which
     * covers every pixel because values inside a run are constant */
    if (it->channels == 2) {
        const unsigned char *p = it->pix + (size_t)row * w * 2;
        prev = map[(p[0] << 8) | p[1]];
        rs[0] = 0; rp[0] = prev; n = 1; any = prev;
        for (int x = 1; x < w; x++) {
            int v = map[(p[2 * x] << 8) | p[2 * x + 1]];
            if (v != prev) {
                rs[n] = x; rp[n] = v; n++;
                prev = v; any |= v;
            }
        }
    }
    else {
        const unsigned char *p = it->pix + (size_t)row * w;
        prev = map[p[0]];
        rs[0] = 0; rp[0] = prev; n = 1; any = prev;
        for (int x = 1; x < w; x++) {
            int v = map[p[x]];
            if (v != prev) {
                rs[n] = x; rp[n] = v; n++;
                prev = v; any |= v;
            }
        }
    }
    if (!any)
        return 0;

    int first = 0;
    while (rp[first] == 0) first++;
    int last = n - 1;
    while (rp[last] == 0) last--;
    int last_end = (last + 1 < n) ? rs[last + 1] : w;

    const int ltr = it->ltr;
    /* Serpentine, as in the reference: a right-to-left pass enters at
     * the rightmost ink edge and targets each run's LEFT edge carrying
     * that run's power. */
    const int entry = ltr ? rs[first] : last_end;
    const int exit_ = ltr ? last_end : rs[first];

    char *base = it->buf, *b = base;
    size_t *off = it->off;
    int lc = 0;
    const char *ysp = it->ys[it->h - 1 - row];
    const int ysn = it->ys_n[it->h - 1 - row];

#define PUT_LIT(lit) (memcpy(b, "" lit, sizeof(lit) - 1), b += sizeof(lit) - 1)
#define PUT(p_, n_)  (memcpy(b, (p_), (size_t)(n_)), b += (n_))
#define END_LINE()   (off[++lc] = (size_t)(b - base))

    off[0] = 0;

    if (it->overscan) {
        const char **oin = ltr ? it->xm : it->xp;
        const int *oin_n = ltr ? it->xm_n : it->xp_n;
        PUT_LIT("G0 X"); PUT(oin[entry], oin_n[entry]);
        PUT_LIT(" Y");   PUT(ysp, ysn);
        PUT_LIT(" S0");  END_LINE();
        PUT_LIT("G1 X"); PUT(it->xs[entry], it->xs_n[entry]);
        PUT_LIT(" S0");  END_LINE();
    }
    else {
        PUT_LIT("G0 X"); PUT(it->xs[entry], it->xs_n[entry]);
        PUT_LIT(" Y");   PUT(ysp, ysn);
        PUT_LIT(" S0");  END_LINE();
    }

    if (ltr) {
        for (int i = first; i <= last; i++) {
            int xe = (i + 1 < n) ? rs[i + 1] : w;  /* run end */
            int pwv = rp[i];
            PUT_LIT("G1 X"); PUT(it->xs[xe], it->xs_n[xe]);
            PUT_LIT(" S");   PUT(it->ss[pwv], it->ss_n[pwv]);
            END_LINE();
        }
    }
    else {
        for (int i = last; i >= first; i--) {
            int xst = rs[i];                       /* run start */
            int pwv = rp[i];
            PUT_LIT("G1 X"); PUT(it->xs[xst], it->xs_n[xst]);
            PUT_LIT(" S");   PUT(it->ss[pwv], it->ss_n[pwv]);
            END_LINE();
        }
    }

    if (it->overscan) {
        const char **oout = ltr ? it->xp : it->xm;
        const int *oout_n = ltr ? it->xp_n : it->xm_n;
        PUT_LIT("G1 X"); PUT(oout[exit_], oout_n[exit_]);
        PUT_LIT(" S0");  END_LINE();
    }

#undef PUT_LIT
#undef PUT
#undef END_LINE

    it->line_count = lc;
    it->line_pos = 0;
    if (it->bidirectional)
        it->ltr = !ltr;
    return 1;
}

static PyObject *
L2L_iternext(L2LIter *it)
{
    while (it->line_pos >= it->line_count) {
        if (it->row < 0)
            return NULL;                /* StopIteration */
        int row = it->row--;
        if (!fill_row(it, row))
            continue;
    }
    size_t start = it->off[it->line_pos];
    size_t len = it->off[it->line_pos + 1] - start;
    it->line_pos++;

    /* every table string and literal is ASCII */
    PyObject *s = PyUnicode_New((Py_ssize_t)len, 127);
    if (!s)
        return NULL;
    memcpy(PyUnicode_1BYTE_DATA(s), it->buf + start, len);
    return s;
}

static PyTypeObject L2LIter_Type = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "_l2l_native.L2LIterator",
    .tp_basicsize = sizeof(L2LIter),
    .tp_dealloc = (destructor)L2L_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_iter = PyObject_SelfIter,
    .tp_iternext = (iternextfunc)L2L_iternext,
};

/* ------------------------------------------------------------------ */

PyDoc_STRVAR(generate_doc,
"generate(pixels, w, h, channels, invert, clip_max, s_min, s_max,\n"
"         xs, ys, ss, bidirectional, xm, xp) -> iterator of str\n\n"
"Body lines of an L2L job (no header/footer). pixels is the raw\n"
"post-resize buffer (L or LA). xs/ys/ss (and xm/xp when overscan is\n"
"active, else None) are Python-preformatted string tables; C never\n"
"formats numbers. Tables must be TUPLES of str: the iterator caches\n"
"pointers into the items and may outlive the calling frame by hours,\n"
"so immutability of the containers is part of the safety contract.");

static PyObject *
l2l_generate(PyObject *self, PyObject *args)
{
    PyObject *pixels;
    int w, h, channels, invert, clip_max, smin, smax, bidirectional;
    PyObject *xs, *ys, *ss, *xm, *xp;

    if (!PyArg_ParseTuple(args, "SiiiiiiiO!O!O!iOO:generate",
                          &pixels, &w, &h, &channels, &invert, &clip_max,
                          &smin, &smax,
                          &PyTuple_Type, &xs, &PyTuple_Type, &ys,
                          &PyTuple_Type, &ss,
                          &bidirectional, &xm, &xp))
        return NULL;

    if (w < 1 || h < 1) {
        PyErr_SetString(PyExc_ValueError, "w and h must be >= 1");
        return NULL;
    }
    if (channels != 1 && channels != 2) {
        PyErr_SetString(PyExc_ValueError, "channels must be 1 (L) or 2 (LA)");
        return NULL;
    }
    if (smin < 0 || smax < smin) {
        PyErr_SetString(PyExc_ValueError, "need 0 <= s_min <= s_max");
        return NULL;
    }
    if (PyBytes_GET_SIZE(pixels) < (Py_ssize_t)w * h * channels) {
        PyErr_SetString(PyExc_ValueError, "pixel buffer shorter than w*h*channels");
        return NULL;
    }
    int overscan = (xm != Py_None);
    if (overscan && (!PyTuple_Check(xm) || !PyTuple_Check(xp))) {
        PyErr_SetString(PyExc_TypeError, "xm and xp must both be tuples or None");
        return NULL;
    }

    L2LIter *it = PyObject_New(L2LIter, &L2LIter_Type);
    if (!it)
        return NULL;
    /* zero everything past the header so dealloc is always safe */
    memset((char *)it + sizeof(PyObject), 0,
           sizeof(L2LIter) - sizeof(PyObject));

    Py_INCREF(pixels);
    it->pixels = pixels;
    it->pix = (const unsigned char *)PyBytes_AS_STRING(pixels);
    it->w = w;
    it->h = h;
    it->channels = channels;
    it->bidirectional = bidirectional != 0;
    it->overscan = overscan;
    it->ltr = 1;
    it->row = h - 1;
    it->line_count = 0;
    it->line_pos = 0;

    it->tables[0] = xs; Py_INCREF(xs);
    it->tables[1] = ys; Py_INCREF(ys);
    it->tables[2] = ss; Py_INCREF(ss);
    it->tables[3] = overscan ? xm : NULL; Py_XINCREF(it->tables[3]);
    it->tables[4] = overscan ? xp : NULL; Py_XINCREF(it->tables[4]);

    it->map = build_power_map(channels, invert != 0, clip_max,
                              (int64_t)smin, (int64_t)(smax - smin));
    if (!it->map)
        goto fail;

    int maxlen = 1;
    if (build_table(xs, (Py_ssize_t)w + 1, "xs", &it->xs, &it->xs_n, &maxlen) < 0 ||
        build_table(ys, (Py_ssize_t)h, "ys", &it->ys, &it->ys_n, &maxlen) < 0 ||
        build_table(ss, (Py_ssize_t)smax + 1, "ss", &it->ss, &it->ss_n, &maxlen) < 0)
        goto fail;
    if (overscan &&
        (build_table(xm, (Py_ssize_t)w + 1, "xm", &it->xm, &it->xm_n, &maxlen) < 0 ||
         build_table(xp, (Py_ssize_t)w + 1, "xp", &it->xp, &it->xp_n, &maxlen) < 0))
        goto fail;

    it->run_start = PyMem_Malloc((size_t)w * sizeof(int));
    it->run_pw = PyMem_Malloc((size_t)w * sizeof(int));
    /* worst case: w move lines + 3 extra lines, each bounded by
     * literals (<16 bytes) plus at most three table strings */
    it->buf = PyMem_Malloc((size_t)(w + 4) * (16 + 3 * (size_t)maxlen));
    it->off = PyMem_Malloc((size_t)(w + 5) * sizeof(size_t));
    if (!it->run_start || !it->run_pw || !it->buf || !it->off) {
        PyErr_NoMemory();
        goto fail;
    }
    return (PyObject *)it;

fail:
    Py_DECREF(it);
    return NULL;
}

static PyMethodDef l2l_methods[] = {
    {"generate", l2l_generate, METH_VARARGS, generate_doc},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef l2l_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_l2l_native",
    .m_doc = "C hot path for pygrbl_build's L2L generator "
             "(byte-identical to the Python reference engine).",
    .m_size = -1,
    .m_methods = l2l_methods,
};

PyMODINIT_FUNC
PyInit__l2l_native(void)
{
    if (PyType_Ready(&L2LIter_Type) < 0)
        return NULL;
    return PyModule_Create(&l2l_module);
}
