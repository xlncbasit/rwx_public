"""pytest plugin for the map build: per test, the tests/ files it opens and the sqlglot modules it newly loads."""
import builtins, io, json, os, sys
import pytest

cur = [None]
rec = {}
acci = set()  # (receiver class, attribute, module being imported) for reads during imports
acc = set()  # (receiver class, attribute) for every read of a class-level constant of a sqlglot class
_SIMPLE = (dict, set, frozenset, list, tuple, str, int, float, bool, bytes, type(None))


class Rec:
    """Non-data descriptor standing in for a class-level constant: same value, but remembers who read it."""
    __slots__ = ("n", "v")

    def __init__(self, n, v):
        self.n, self.v = n, v

    def __get__(self, obj, owner=None):
        if not _imp[0]:
            acc.add((owner if owner is not None else type(obj), self.n))
        else:  # read while a module is being imported: baked into that module's state, which the diff compares
            acci.add((owner if owner is not None else type(obj), self.n, _impname[-1]))
        return self.v


def _wrap_cls(cls):
    if not getattr(cls, "__module__", "").startswith("sqlglot") or type(cls).__module__ not in ("builtins",) and not type(cls).__module__.startswith("sqlglot"):
        return
    for n, v in list(cls.__dict__.items()):
        if type(v) in _SIMPLE and not (n.startswith("__") and n.endswith("__")):
            type.__setattr__(cls, n, Rec(n, v))


_bc = builtins.__build_class__


def _build_class(func, name, *bases, **kw):
    cls = _bc(func, name, *bases, **kw)
    try:
        _wrap_cls(cls)
    except Exception:
        pass
    return cls


builtins.__build_class__ = _build_class


def _acc_dump():
    out, mro = set(), {}
    for T, n in list(acc):
        q = f"{T.__module__}.{T.__qualname__}"
        out.add((q, n))
        if q not in mro:
            mro[q] = [f"{c.__module__}.{c.__qualname__}" for c in T.__mro__ if c.__module__.startswith("sqlglot")]
    ai = sorted({(f"{T.__module__}.{T.__qualname__}", n, m) for T, n, m in list(acci)})
    return sorted(out), mro, ai
_open = builtins.open


def _wrap(orig):
    def op(file, *a, **k):
        try:
            if cur[0] and isinstance(file, (str, os.PathLike)):
                p = os.fspath(file)
                if "/tests/" in os.path.abspath(p) or p.startswith("tests/"):
                    rec[cur[0]]["fix"].add(os.path.relpath(os.path.abspath(p), "/opt/sqlglot"))
        except Exception:
            pass
        return orig(file, *a, **k)
    return op


builtins.open = _wrap(builtins.open)
io.open = builtins.open


_types = set()
_mro = {}


def _hook_inits():
    """Record which sqlglot classes (with their whole MRO, as module.qualname) each test instantiates."""
    import importlib
    for mod, cls in (("sqlglot.dialects.dialect", "Dialect"), ("sqlglot.parser", "Parser"), ("sqlglot.generator", "Generator"),
                     ("sqlglot.tokens", "Tokenizer"), ("sqlglot.expressions.core", "Expr")):
        try:
            C = getattr(importlib.import_module(mod), cls)
        except Exception:
            continue
        orig = C.__init__

        def init(self, *a, __o=orig, **k):
            _types.add(type(self))
            return __o(self, *a, **k)
        C.__init__ = init


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    """Importing a test module can run sqlglot code (module-level parses): charge it to that file's tests."""
    mod = isinstance(collector, pytest.Module)
    if mod:
        _sw("S:" + collector.nodeid)
    yield
    if mod:
        _sw(None)


def pytest_configure(config):
    import sqlglot  # noqa: F401  (import-time lines belong to everybody, not to the first file collected)
    _hook_inits()
    _patch_pool()


ctx = [None]
_child_ready = [False]
_cc = [""]  # coverage context currently active
_imp = [0]  # >0 while a sqlglot module body is executing (import time)
_stack = []
_impname = [""]


def _swc(name):
    import coverage
    _cc[0] = name
    c = coverage.Coverage.current()
    if c:
        c.switch_context(name)


def _sw(name):
    ctx[0] = name
    _swc(name or "")


def _install_exec_hook():
    import importlib._bootstrap_external as be
    orig = be._LoaderBasics.exec_module

    def exec_module(self, module):
        n = getattr(module, "__name__", "")
        if not n.startswith("sqlglot"):
            return orig(self, module)
        _imp[0] += 1
        _impname.append(n)
        _stack.append(_cc[0])
        _swc("I:" + n)  # lines run while this module is being imported
        try:
            return orig(self, module)
        finally:
            _imp[0] -= 1
            _impname.pop()
            _swc(_stack.pop())
    be._LoaderBasics.exec_module = exec_module


_install_exec_hook()


def _run(name, fn, *a, **k):
    """Runs inside a pool worker: attribute its coverage and class use to the test that submitted the work."""
    import coverage
    if not _child_ready[0]:
        _hook_inits()
        _child_ready[0] = True
    _swc(name)
    _types.clear()
    try:
        return fn(*a, **k)
    finally:
        used = set()
        for t in _types:
            m = _mro.get(t)
            if m is None:
                m = _mro[t] = [f"{c.__module__}.{c.__qualname__}" for c in t.__mro__ if c.__module__.startswith("sqlglot")]
            used.update(m)
        fd = os.open(os.path.join(os.environ["MAP_CHILD_DIR"], f"child.{os.getpid()}.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        p, m, ai = _acc_dump()
        os.write(fd, (json.dumps({"t": name, "u": sorted(used), "a": p, "m": m, "ai": ai}) + "\n").encode())
        os.close(fd)


def _patch_pool():
    from concurrent.futures import ProcessPoolExecutor
    orig = ProcessPoolExecutor.submit

    def submit(self, fn, /, *a, **k):
        if ctx[0] is not None:
            return orig(self, _run, ctx[0], fn, *a, **k)
        return orig(self, fn, *a, **k)
    ProcessPoolExecutor.submit = submit


_sh = [None, None]
_cur_test = [None]
base = [set()]


def _shared(item):
    """Setup/teardown work (setUpClass etc.) is shared: record it under the parent, expanded to its tests later."""
    k = "S:" + item.nodeid.rsplit("::", 1)[0]
    rec.setdefault(k, {"fix": set(), "mods": set(), "used": set()})
    _sh[0], _sh[1] = k, set(sys.modules)
    cur[0] = k


def _shared_end():
    k = _sh[0]
    for t in _types:
        m = _mro.get(t)
        if m is None:
            m = _mro[t] = [f"{c.__module__}.{c.__qualname__}" for c in t.__mro__ if c.__module__.startswith("sqlglot")]
        rec[k]["used"].update(m)
    _types.clear()
    new = {m for m in set(sys.modules) - _sh[1] if m.startswith("sqlglot")}
    rec[k]["mods"] |= new
    base[0] |= new
    cur[0] = _cur_test[0]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _sw("S:" + item.nodeid.rsplit("::", 1)[0])  # class-/module-level setup is shared by the whole class/file
    _shared(item)
    yield
    _shared_end()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _sw(item.nodeid)  # setUp + test body + tearDown
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _sw("S:" + item.nodeid.rsplit("::", 1)[0])
    _shared(item)
    yield
    _shared_end()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    cur[0] = _cur_test[0] = item.nodeid
    rec[item.nodeid] = {"fix": set(), "mods": set(), "used": set()}
    base[0] = set()
    before = set(sys.modules)
    _types.clear()
    yield
    used = rec[item.nodeid]["used"]
    for t in _types:
        m = _mro.get(t)
        if m is None:
            m = _mro[t] = [f"{c.__module__}.{c.__qualname__}" for c in t.__mro__ if c.__module__.startswith("sqlglot")]
        used.update(m)
    rec[item.nodeid]["mods"] = {m for m in set(sys.modules) - before if m.startswith("sqlglot")} - base[0]
    cur[0] = None


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("MAP_ISO"):  # one test per process: everything that got imported belongs to it
        allm = {m for m in sys.modules if m.startswith("sqlglot")}
        for v in rec.values():
            v["mods"] = allm
    p, m, ai = _acc_dump()
    with open(os.environ["MAP_OUT"], "w") as f:
        json.dump({"__acc__": {"a": p, "m": m, "ai": ai}, **{k: {"fix": sorted(v["fix"]), "mods": sorted(v["mods"]), "used": sorted(v["used"])} for k, v in rec.items()}}, f)
