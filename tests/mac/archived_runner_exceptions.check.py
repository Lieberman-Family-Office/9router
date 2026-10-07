"""Check archived handler dispatch without executing the runners' jobs."""

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
    "task-4-green-959c577a870a-8a365ef0",
]
PATHS = [
    ROOT / "docs/superpowers/evidence" / run / "guest-runner.py" for run in RUNS
] + [ROOT / "docs/superpowers/evidence/task-2-2026-10-05-ef67fdaa/host-orchestrator.py"]

for path in PATHS:
    tree = ast.parse(path.read_text())
    for handler in (
        node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
    ):
        assert handler.type is not None, f"Bare handler: {path}:{handler.lineno}"
        assert not any(
            isinstance(node, ast.Name) and node.id == "BaseException"
            for node in ast.walk(handler.type)
        ), f"Broad handler: {path}:{handler.lineno}"
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
        except (RuntimeError, KeyboardInterrupt, SystemExit) as caught:
            assert caught is error, f"Changed subject exception: {path}"
            propagated = True
        assert propagated == isinstance(error, (KeyboardInterrupt, SystemExit)), path
        assert events.count("cleanup") == 1, f"Cleanup did not run once: {path}"
        if isinstance(error, RuntimeError):
            assert events == ["handled", "cleanup"], path

print(f"PASS: handler dispatch and cleanup in {len(PATHS)} archived runners; 21 cases")
