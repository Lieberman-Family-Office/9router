import json, os
from pathlib import Path
records=[]
collected=[]
collection=[]
def pytest_collection_modifyitems(items):
    collected.extend(item.nodeid for item in items)
def pytest_collectreport(report):
    if report.failed: collection.append({'nodeid':report.nodeid,'message':str(report.longrepr)})
def pytest_runtest_logreport(report):
    records.append({'nodeid':report.nodeid,'phase':report.when,'outcome':report.outcome,'message':str(report.longrepr) if report.failed else None})
def pytest_sessionfinish(session, exitstatus):
    Path(os.environ['CHECK_REPORT']).write_text(json.dumps({'exitCode':int(exitstatus),'collected':collected,'collectionFailures':collection,'reports':records},indent=2))
