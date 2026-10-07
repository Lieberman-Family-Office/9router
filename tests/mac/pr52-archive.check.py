"""Run: python3 tests/mac/pr52-archive.check.py. No services or runner jobs execute."""

import ast
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/superpowers/evidence"
plugin_path = EVIDENCE / "task-4-red-959c577a870a-652526fc/checkpoint_plugin.py"
spec = importlib.util.spec_from_file_location("checkpoint", plugin_path)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
plugin.records.append("stale")
plugin.pytest_sessionstart(None)
assert plugin.records == plugin.collected == plugin.collection == []
session = SimpleNamespace(exitstatus=0)
with patch.dict(os.environ, {}, clear=True):
    plugin.pytest_sessionfinish(session, 0)
assert session.exitstatus == 3

runner_path = EVIDENCE / "task-2-retry-1f8d2e5d7825-ea76e6c8/guest-runner.py"
tree = ast.parse(runner_path.read_text())
unit = next(
    n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "unit_summary"
)
namespace = {"json": json, "digest": lambda _: "fixture"}
exec(
    compile(ast.Module(body=[unit], type_ignores=[]), str(runner_path), "exec"),
    namespace,
)
report = {
    "testResults": [{"status": "passed", "assertionResults": [{"status": "passed"}]}],
    "numTotalTests": 1,
    "numPendingTests": 0,
    "numFailedTests": 0,
    "numTodoTests": 0,
    "success": True,
}
reader = SimpleNamespace(read_text=lambda: json.dumps(report))
assert namespace["unit_summary"](reader, 1, 1)("")["testCount"] == 1
for mutate in (
    lambda: report.update(numTodoTests=1),
    lambda: report["testResults"][0]["assertionResults"][0].update(status="skipped"),
):
    report["numTodoTests"] = 0
    mutate()
    try:
        namespace["unit_summary"](reader, 1, 1)("")
    except AssertionError:
        pass
    else:
        raise AssertionError("Incomplete assertion population was accepted")
print(
    "PASS: plugin resets subjects and refuses missing output; "
    "validator rejects todo and skipped assertions"
)
