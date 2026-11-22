# Blast Radius: test selection for sqlglot patches

For each patch, print the tests it can break and split them over workers (`selector.py`, stdlib only).

How it decides (everything is built from the pinned sqlglot commit, nothing is looked up by patch):

- **Per-test maps** (`data/map.json.gz`, built by `tools/mapiso.sh`): every test is run alone in its own process under coverage, recording the lines it runs outside module imports, the lines run while a module is imported (and which module), the fixture files it opens, the sqlglot classes it instantiates, and which class-level constants it reads at run time.
- **Function bodies**: a changed statement selects the tests that ran it. If the line also runs at import time, the module state is compared as below.
- **Class and module level** (constants, `arg_types`, new members): `snapshot.py` imports the old and the patched module and compares what they define; the tests that read the changed constants are selected.
- **Fixtures**: the tests that opened the changed file.
- **No-ops**: comment-, docstring- and annotation-only changes and `is None` / `== None` rewrites select nothing.
- Anything it cannot map (unparsable file, new or deleted files, test-helper edits, errors) runs everything.
- **TPC tests proven by replay** (`replay.py`, map `data/replay.json.gz` from `tools/replay_map.py`): `test_execute_tpcds`, `test_execute_tpch` and `test_optimized_tpch` check fixture queries one by one. The map records, per query and phase (parse/transpile, optimizer, execution), the lines it reaches, the class constants it reads, and hashes of its results. A selected test is dropped when replaying the affected queries with the patched sources gives identical SQL for duckdb and identical results. The replay stops at the first checkpoint (parsed tree, optimized tree) after which nothing changed is reached, within a CPU budget.
- **Value map** (`data/vmap.json.gz`, built by `tools/valiso.sh` with the `tools/valplug.py` import-time instrumentation, one isolated pytest process per test). For every `<`/`<=`/`>`/`>=` comparison it records the operand relations each test saw; for every `and`/`or` used only for truthiness, whether all operands were truthy or all falsy. When a patch only swaps such an operator on one line, tests on which the new operator gives the same result on every recorded evaluation are dropped. Tests whose instrumented run did not pass, or that use worker processes, are never dropped.
- **Small selections are run** (`runner.py`): when every change is plain code inside sqlglot function bodies and the whole selection is light (≤ 1 s recorded), in-process (no worker pools or subprocesses), the selector forks itself, swaps the patched functions' code into the already imported modules (other modules load from the patched sources), runs the tests with `unittest` and keeps only those that fail (stopping at the first failure and keeping the rest). Anything unexpected keeps the whole selection.
- **Split**: LPT over the chosen tests, with the worker count that minimises busiest worker + 2 s per worker.

`docker build .` fetches sqlglot at the pinned commit and byte-compiles it; `tools/` rebuilds the map in a lab image (`tools/lab.Dockerfile`).
