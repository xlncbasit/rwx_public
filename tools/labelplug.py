"""pytest plugin: write the ids of failing tests (failed / error / timeout / subtest failure / uncollectable file)."""
import json, os

failed, filefail, seen = set(), set(), set()


def pytest_collectreport(report):
    if report.failed:
        filefail.add(report.nodeid)


def pytest_runtest_logreport(report):
    seen.add(report.nodeid)
    if report.failed:
        failed.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["LAB_OUT"], "w") as f:
        json.dump({"failed": sorted(failed), "filefail": sorted(filefail), "ran": len(seen)}, f)
