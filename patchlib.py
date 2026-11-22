"""Tiny unified-diff reader/applier (stdlib only; shared by the selector and the labeling tools)."""
import re
from dataclasses import dataclass, field

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FilePatch:
    old: str | None = None  # path in the old tree (None: file is new)
    new: str | None = None  # path in the new tree (None: file is deleted)
    hunks: list = field(default_factory=list)  # (old_start, old_len, new_start, new_len, [(tag, text)])
    binary: bool = False

    @property
    def path(self):
        return self.new or self.old


def _strip(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse(text):
    out, cur, hunk = [], None, None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FilePatch(old=m.group(1) if m else None, new=m.group(2) if m else None)
            out.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif hunk is None and line.startswith("--- "):
            cur.old = _strip(line[4:])
        elif hunk is None and line.startswith("+++ "):
            cur.new = _strip(line[4:])
        elif line.startswith("new file mode"):
            cur.old = None
        elif line.startswith("deleted file mode"):
            cur.new = None
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.binary = True
        elif line.startswith("rename from "):
            cur.old = line[12:]
        elif line.startswith("rename to "):
            cur.new = line[10:]
        else:
            m = HUNK.match(line)
            if m:
                hunk = (int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1), [])
                cur.hunks.append(hunk)
            elif hunk is not None and line[:1] in (" ", "+", "-"):
                hunk[4].append((line[0], line[1:]))
            elif hunk is not None and line.startswith("\\"):
                hunk[4].append(("\\", line))
    return out


def apply(old_text, fp):
    """New text of a file given its old text (`""` for a new file) and its FilePatch."""
    old = old_text.split("\n")
    out, pos = [], 0
    for os_, ol, ns, nl, lines in fp.hunks:
        start = os_ - 1 if ol else os_
        out += old[pos:start]
        pos = start
        for tag, t in lines:
            if tag == " ":
                out.append(old[pos]); pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                out.append(t)
    out += old[pos:]
    return "\n".join(out)
