"""Import some sqlglot modules (optionally with patched sources) and print a canonical hash of every class-level
constant and module global. Comparing the output for the old and the patched tree shows which constants really change.
stdin: {"mods": [module, ...], "patched": {"sqlglot/x.py": "<new source>"}}; stdout: JSON."""
import functools, hashlib, importlib, importlib.abc, importlib.util, json, os, re, sys, types

ROOT = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")
sys.path.insert(0, ROOT)
sys.dont_write_bytecode = True


class _Loader(importlib.abc.SourceLoader):
    def __init__(self, path, src):
        self.path, self.src = path, src.encode()

    def get_filename(self, name):
        return self.path

    def get_data(self, path):
        return self.src


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, patched):
        self.by_name = {}
        for rel, src in patched.items():
            n = rel[:-3].replace("/", ".")
            n = n[:-9] if n.endswith(".__init__") else n
            self.by_name[n] = (os.path.join(ROOT, rel), src)

    def find_spec(self, name, path=None, target=None):
        if name in self.by_name:
            p, src = self.by_name[name]
            return importlib.util.spec_from_file_location(name, p, loader=_Loader(p, src))
        return None


def code_key(c, depth=0):
    parts = [c.co_code.hex(), repr(c.co_names)]
    for k in c.co_consts:
        parts.append("code:" + code_key(k, depth + 1) if isinstance(k, types.CodeType) else canon(k, depth + 1))
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def canon(v, d=0):
    if d > 6:
        return "deep"
    t = type(v)
    if v is None or t in (bool, int, float, str, bytes, complex):
        return repr(v)
    if t is dict:
        return "{" + ",".join(sorted(canon(k, d + 1) + ":" + canon(x, d + 1) for k, x in v.items())) + "}"
    if t in (list, tuple):
        return "[" + ",".join(canon(x, d + 1) for x in v) + "]"
    if t in (set, frozenset):
        return "s{" + ",".join(sorted(canon(x, d + 1) for x in v)) + "}"
    if isinstance(v, type):
        return "cls:" + v.__module__ + "." + v.__qualname__
    if isinstance(v, types.FunctionType):
        cells = []
        for c in v.__closure__ or ():
            try:
                cells.append(canon(c.cell_contents, d + 1))
            except ValueError:  # empty cell
                cells.append("empty")
        return "fn:" + code_key(v.__code__) + canon(v.__defaults__, d + 1) + canon(v.__kwdefaults__, d + 1) + "<" + ",".join(cells) + ">"
    if isinstance(v, functools.partial):
        return "partial:" + canon(v.func, d + 1) + canon(v.args, d + 1) + canon(v.keywords, d + 1)
    if isinstance(v, types.MethodType):
        return "meth:" + canon(v.__func__, d + 1)
    if isinstance(v, (classmethod, staticmethod)):
        return t.__name__ + canon(v.__func__, d + 1)
    if isinstance(v, types.ModuleType):
        return "mod:" + v.__name__
    if isinstance(v, re.Pattern):
        return "re:" + v.pattern + str(v.flags)
    r = repr(v)
    return "obj:" + t.__qualname__ + ("" if " at 0x" in r else r)


SIMPLE_ATTR = (dict, set, frozenset, list, tuple, str, int, float, bool, bytes, type(None), re.Pattern)


def cls_state(cls):
    st = {"<mro>": ",".join(f"{c.__module__}.{c.__qualname__}" for c in cls.__mro__)}
    fns = []
    for n, v in cls.__dict__.items():
        if n.startswith("__") and n.endswith("__") and n not in ("__slots__",):
            continue
        if isinstance(v, SIMPLE_ATTR) or isinstance(v, type) or v.__class__.__name__ == "Rec":
            st[n] = hashlib.md5(canon(v).encode()).hexdigest()[:12]
        elif callable(v) or isinstance(v, (classmethod, staticmethod, property)):
            fns.append(n)
        else:
            st[n] = hashlib.md5(canon(v).encode()).hexdigest()[:12]
    st["<fns>"] = ",".join(sorted(fns))
    return st


def walk(cls, mod, out):
    out[f"{cls.__module__}.{cls.__qualname__}"] = cls_state(cls)
    for v in list(cls.__dict__.values()):
        if isinstance(v, type) and v.__module__ == mod and v.__qualname__.startswith(cls.__qualname__ + "."):
            walk(v, mod, out)


def run(spec):
    sys.meta_path.insert(0, _Finder(spec.get("patched", {})))
    errs = {}
    for m in spec["mods"]:  # a module that no longer imports breaks every test that loads it
        try:
            importlib.import_module(m)
        except BaseException as e:
            errs[m] = repr(e)[:200]
    cls, glob = {}, {}
    for m in spec["mods"]:
        mod = sys.modules.get(m)
        if mod is None:
            continue
        g = glob[m] = {}
        for n, v in list(vars(mod).items()):
            if n.startswith("__") and n.endswith("__"):
                continue
            if isinstance(v, type) and v.__module__ == m:
                walk(v, m, cls)
                g[n] = "class"
            elif isinstance(v, types.FunctionType):
                g[n] = "fn"
            else:
                g[n] = hashlib.md5(canon(v).encode()).hexdigest()[:12]
    return {"ok": True, "errs": errs, "cls": cls, "glob": glob}


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin))))
