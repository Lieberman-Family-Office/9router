import json
import os
from pathlib import Path

records = []
collected = []
collection = []


def pytest_sessionstart(session):
    records.clear()
    collected.clear()
    collection.clear()


def pytest_collection_modifyitems(items):
    collected.extend(item.nodeid for item in items)


def pytest_collectreport(report):
    if report.failed:
        collection.append({"nodeid": report.nodeid, "message": str(report.longrepr)})


def pytest_runtest_logreport(report):
    records.append(
        {
            "nodeid": report.nodeid,
            "phase": report.when,
            "outcome": report.outcome,
            "message": str(report.longrepr) if report.failed else None,
        }
    )


def pytest_sessionfinish(session, exitstatus):
    target = os.environ.get("CHECK_REPORT")
    if not target:
        session.exitstatus = 3
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        target += f".{worker}.{os.getpid()}"
    try:
        Path(target).write_text(
            json.dumps(
                {
                    "exitCode": int(exitstatus),
                    "collected": collected,
                    "collectionFailures": collection,
                    "reports": records,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        session.exitstatus = 3
