"""Inside the lab container: run each test of a shard in its own process under coverage.
usage: isorun.py SHARD_JSON OUT_DIR   (SHARD_JSON: [[number, nodeid], ...]); writes OUT_DIR/<number>.json"""
import glob, json, os, shutil, subprocess, sys
from coverage import CoverageData

shard, out = json.load(open(sys.argv[1])), sys.argv[2]
os.makedirs(out, exist_ok=True)
for num, nid in shard:
    if os.path.exists(f"{out}/{num}.json"):
        continue
    w = f"/tmp/iso{num}"
    shutil.rmtree(w, ignore_errors=True); os.makedirs(w)
    env = dict(os.environ, COVERAGE_FILE=f"{w}/cov", COVERAGE_PROCESS_START="/tools/cov.rc", MAP_CHILD_DIR=w,
               MAP_OUT=f"{w}/map.json", MAP_ISO="1", PYTHONPATH="/opt/sqlglot:/tools")
    r = subprocess.run(["python", "-m", "coverage", "run", "--rcfile=/tools/cov.rc", "-m", "pytest", "-p", "mapplug", "-p", "no:cacheprovider",
                        "-q", "--no-header", "--timeout=900", nid], cwd="/opt/sqlglot", env=env, capture_output=True, text=True)
    cov, imp = {}, {}  # cov: lines run outside module imports; imp: line -> modules whose import ran it
    for p in glob.glob(f"{w}/cov.*"):
        try:
            cd = CoverageData(p); cd.read()
            for f in cd.measured_files():
                rel = f.replace("/opt/sqlglot/", "")
                for ln, ctxs in cd.contexts_by_lineno(f).items():
                    for c in ctxs:
                        if c.startswith("I:"):
                            imp.setdefault(rel, {}).setdefault(ln, set()).add(c[2:])
                        else:
                            cov.setdefault(rel, set()).add(ln)
        except Exception:
            pass
    used, pairs, mro, ai = set(), set(), {}, set()
    for p in glob.glob(f"{w}/child.*.jsonl"):
        for line in open(p):
            r_ = json.loads(line); used.update(r_["u"]); pairs.update(map(tuple, r_["a"])); mro.update(r_["m"]); ai.update(map(tuple, r_["ai"]))
    try:
        m = json.load(open(f"{w}/map.json"))
    except Exception:
        m = {}
    a = m.pop("__acc__", {"a": [], "m": {}, "ai": []})
    ai.update(map(tuple, a["ai"]))
    pairs.update(map(tuple, a["a"])); mro.update(a["m"])
    fix, mods, us = set(), set(), set(used)
    for v in m.values():
        fix.update(v["fix"]); mods.update(v["mods"]); us.update(v["used"])
    json.dump({"cov": {f: sorted(l) for f, l in cov.items()}, "imp": {f: {l: sorted(m) for l, m in d.items()} for f, d in imp.items()}, "fix": sorted(fix), "mods": sorted(mods), "used": sorted(us),
               "acc": sorted(pairs), "acci": sorted(ai), "mro": mro, "rc": r.returncode}, open(f"{out}/{num}.json", "w"))
    shutil.rmtree(w, ignore_errors=True)
