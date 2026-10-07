"""Check handler dispatch in five archived runners without executing their jobs."""

import ast
import copy
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
RUNS = [
    "task-2-2026-10-05-ef67fdaa",
    "task-2-retry-1f8d2e5d7825-7f73c805",
    "task-2-retry-1f8d2e5d7825-ea76e6c8",
    "task-2-retry-f76093a4f8ec-de12e533",
    "task-4-red-959c577a870a-652526fc",
]

for run in RUNS:
    path = ROOT / "docs/superpowers/evidence" / run / "guest-runner.py"
    tree = ast.parse(path.read_text())
    blocks = [node for node in tree.body if isinstance(node, ast.Try) and node.handlers]
    assert blocks, f"No handler subject: {path}"
    block = copy.deepcopy(blocks[-1])
    assert block.finalbody, f"Missing cleanup: {path}"
    block.body = ast.parse("raise error").body
    block.orelse = []
    block.finalbody = ast.parse("events.append('cleanup')").body
    for handler in block.handlers:
        raises = [node for node in handler.body if isinstance(node, ast.Raise)]
        handler.body = ast.parse("events.append('handled')").body + raises
    module = ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[]))
    code = compile(module, str(path), "exec")
    for error in (RuntimeError("operational"), KeyboardInterrupt(), SystemExit(7)):
        events = []
        propagated = False
        try:
            exec(code, {"error": error, "events": events})
        except (KeyboardInterrupt, SystemExit) as caught:
            assert caught is error, f"Changed control exception: {path}"
            propagated = True
        assert propagated == isinstance(error, (KeyboardInterrupt, SystemExit)), path
        assert events.count("cleanup") == 1, f"Cleanup did not run once: {path}"
        if isinstance(error, RuntimeError):
            assert events == ["handled", "cleanup"], path

print(f"PASS: handler dispatch and cleanup in {len(RUNS)} archived runners; 15 cases")
