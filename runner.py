"""Runs selected sqlglot tests against patched sources and reports which fail.

Used two ways:
- in a forked child of the selector (`hot_run`): sqlglot modules already imported get the patched functions'
  code objects swapped in place; modules not imported yet are served from the patched sources on import;
- as a script (tools/runner_eval.py): stdin {"patched": {path: source}, "tests": [{"id", "limit"}]}.
A test fails as pytest would count it: failure, error, failed subtest, unexpected success, its file not importing,
or running past its time limit.
"""
import gc, importlib, json, os, signal, sys, types, unittest

ROOT = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")


class Timeout(BaseException):
    pass


class CannotPatch(Exception):
    pass


def _alarm(signum, frame):
    raise Timeout()


def _codes(code, out):
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            out.setdefault(c.co_qualname, []).append(c)
            _codes(c, out)
    return out


def hot_patch(patched):
    """Swap the patched functions' code into the function objects of already imported modules."""
    want = {}
    for rel, src in patched.items():
        mod = rel[:-3].replace("/", ".")
        mod = mod[:-9] if mod.endswith(".__init__") else mod
        if mod in sys.modules:
            path = os.path.join(ROOT, rel)
            want[path] = _codes(compile(src, path, "exec"), {})
    if not want:
        return
    old = {}
    for o in gc.get_objects():  # every function object, including ones built by closures at import time
        if type(o) is types.FunctionType and o.__code__.co_filename in want:
            old.setdefault((o.__code__.co_filename, o.__code__.co_qualname), []).append(o)
    for (path, q), fns in old.items():
        new = sorted(want[path].get(q, ()), key=lambda c: c.co_firstlineno)
        if not new:
            raise CannotPatch(q)
        olds = sorted({f.__code__ for f in fns}, key=lambda c: c.co_firstlineno)
        if len(olds) != len(new):
            raise CannotPatch(q)
        by = dict(zip(olds, new))
        for f in fns:
            c = by[f.__code__]
            if c.co_freevars != f.__code__.co_freevars:
                raise CannotPatch(q)
            f.__code__ = c


def run_tests(tests, stop_first=False):
    """tests: [(id, limit)] -> (failed ids, ids not run)."""
    signal.signal(signal.SIGALRM, _alarm)
    fail, done = set(), set()
    for k, (tid, limit) in enumerate(tests):
        if stop_first and fail:
            break
        path, cls, meth = tid.split("::")
        done.add(tid)
        try:
            C = getattr(importlib.import_module(path[:-3].replace("/", ".")), cls)
        except BaseException:  # the file does not import: pytest cannot collect it
            fail.add(tid)
            continue
        res = unittest.TestResult()
        signal.setitimer(signal.ITIMER_REAL, limit)
        try:
            unittest.TestSuite([C(meth)]).run(res)
        except Timeout:
            fail.add(tid)
            continue
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        if res.failures or res.errors or res.unexpectedSuccesses or not res.wasSuccessful():
            fail.add(tid)
    return fail, {t for t, _ in tests} - done


def prepare(patched, evict=None):
    """Make the patched sources what every import sees. evict: already imported modules to drop and import again
    from the (patched) sources -- the changed ones and everything importing them; None: swap function code instead."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import snapshot

    sys.meta_path.insert(0, snapshot._Finder(patched))
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    if evict is None:
        hot_patch(patched)
        return
    if evict:  # a loaded module changed: drop all of sqlglot (partial eviction leaves stale cross-module references)
        for m in [m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot.")]:
            del sys.modules[m]
        importlib.import_module("sqlglot")


if __name__ == "__main__":
    try:
        spec = json.load(sys.stdin)
        prepare(spec.get("patched", {}))
        fail, _ = run_tests([(t["id"], t["limit"]) for t in spec["tests"]])
        out = sorted(fail)
    except BaseException:
        out = None
    sys.stdout.flush()
    print("\n" + json.dumps({"fail": out}))
