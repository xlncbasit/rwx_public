"""Inside the lab container: run each test of a shard in its own pytest process with valplug (value map).
usage: valrun.py SHARD_JSON OUT_DIR   (SHARD_JSON: [[number, nodeid], ...]); writes OUT_DIR/<number>.json"""
import json, os, subprocess, sys

shard, out = json.load(open(sys.argv[1])), sys.argv[2]
if os.environ.get("REVERSE"):
    shard = shard[::-1]
os.makedirs(out, exist_ok=True)
env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
env.update(PYTHONPATH="/opt/sqlglot:/tools", PYTHONPYCACHEPREFIX="/tmp/valpyc", PYTHONHASHSEED="0")
for num, nid in shard:
    dst = f"{out}/{num}.json"
    if os.path.exists(dst):
        continue
    tmp = f"/tmp/val{num}.json"
    r = subprocess.run(["python", "-m", "pytest", "-p", "valplug", "-p", "no:cacheprovider", "-q", "--no-header", "--timeout=900", nid],
                       cwd="/opt/sqlglot", env={**env, "VAL_OUT": tmp}, capture_output=True, text=True)
    try:
        d = json.load(open(tmp))
    except Exception:
        d = {"cmp": {}, "bool": {}, "exit": -1}
    d["rc"] = r.returncode
    json.dump(d, open(dst, "w"))
    print(num, r.returncode, flush=True)
