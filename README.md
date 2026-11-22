# Blast Radius: test selection for sqlglot patches

For each patch, print the tests it can break and split them over workers (`selector.py`, stdlib only).

How it decides (everything is built from the pinned sqlglot commit, nothing is looked up by patch):

- **Per-test maps** (`data/map.json.gz`, built by `tools/mapiso.sh`): every test is run alone in its own process under coverage, recording the lines it runs outside module imports, the lines run while a module is imported (and which module), the fixture files it opens, the sqlglot classes it instantiates, and which class-level constants it reads at run time.
- **Function bodies**: a changed statement selects the tests that ran it. If the line also runs at import time, the module state is compared as below.
- **Class and module level** (constants, `arg_types`, new members): `snapshot.py` imports the old and the patched module and compares what they define; the tests that read the changed constants are selected.
- **Fixtures**: the tests that opened the changed file.
- **No-ops**: comment-, docstring- and annotation-only changes and `is None` / `== None` rewrites select nothing.
- Anything it cannot map (unparsable file, new or deleted files, test-helper edits, errors) runs everything.
- **Split**: LPT over the chosen tests, with the worker count that minimises busiest worker + 2 s per worker.

`docker build .` fetches sqlglot at the pinned commit and byte-compiles it; `tools/` rebuilds the map in a lab image (`tools/lab.Dockerfile`).
