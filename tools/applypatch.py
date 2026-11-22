import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import patchlib
for fp in patchlib.parse(open(sys.argv[1]).read()):
    if fp.binary: continue
    if fp.new is None:
        os.remove(fp.old); continue
    old = open(fp.old).read() if fp.old and os.path.exists(fp.old) else ""
    os.makedirs(os.path.dirname(fp.new) or ".", exist_ok=True)
    open(fp.new, "w").write(patchlib.apply(old, fp))
    if fp.old and fp.old != fp.new and os.path.exists(fp.old): os.remove(fp.old)
