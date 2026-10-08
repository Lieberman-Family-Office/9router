#!/usr/bin/env python3
"""Exercise actual acquisition ASTs without activating Namespace or reading credentials."""
import ast
import collections
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/superpowers/evidence/task5-continuity-recovery-d6ba92d8"
FILES = (
    "host-landing-checks.py", "host-persistence-proof.py", "host-reproduce.py",
    "host-root-workspace-proof.py", "host-storage-inspect.py", "host-signin.py",
)

class Fault(BaseException):
    pass

class Claims(list):
    def append(self, value):
        if fault == "append" and opened[-1] == 102:
            raise Fault()
        super().append(value)

def acquire(*_args):
    descriptor = 101 + len(opened)
    opened.append(descriptor)
    return descriptor

def close(descriptor):
    closed.append(descriptor)

def action(stage, descriptor):
    if fault == stage and descriptor == 102:
        raise Fault()

population = 0
for filename in FILES:
    tree = ast.parse((EVIDENCE / filename).read_text())
    outer = next(node for node in tree.body if isinstance(node, ast.Try) and any(
        isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        and child.func.attr == "open" for child in ast.walk(node)
    ))
    loop = next(node for node in ast.walk(outer) if isinstance(node, ast.For)
                and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "open" for child in ast.walk(node)))
    descriptor_name = next(node.targets[0].id for node in loop.body if isinstance(node, ast.Assign))
    target_name = loop.target.id
    module = ast.fix_missing_locations(ast.Module(body=loop.body, type_ignores=[]))
    for fault in ("append", "flock", "write", "none"):
        opened, closed, claims = [], [], Claims()
        namespace = {
            "os": SimpleNamespace(open=acquire, close=close, write=lambda fd, _data: action("write", fd),
                                  O_RDWR=1, O_CREAT=2, O_EXCL=4, getpid=lambda: 1),
            "fcntl": SimpleNamespace(flock=lambda fd, _flags: action("flock", fd), LOCK_EX=1, LOCK_NB=2),
            "claims": claims, "json": json, "owner": "fictional", "run_id": "fictional", "quote": "fictional",
        }
        try:
            for target in ("fictional-a", "fictional-b"):
                namespace[target_name] = target
                exec(compile(module, str(EVIDENCE / filename), "exec"), namespace)
        except Fault:
            pass
        finally:
            for descriptor, _target in claims:
                close(descriptor)
        assert collections.Counter(closed) == collections.Counter(opened), (filename, fault, opened, closed)
        assert all(count == 1 for count in collections.Counter(closed).values())
        assert len(opened) == 2
        population += 1

qualifier = ast.parse((ROOT / "scripts/mac/9router_vm_qualify.py").read_text())
run = next(node for node in qualifier.body if isinstance(node, ast.FunctionDef) and node.name == "cmd_run")
owner = next(node for node in run.body if isinstance(node, ast.With))
acquisition = owner.body[0]
assert isinstance(acquisition, ast.Try)
assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
           and node.func.attr == "close" for node in ast.walk(acquisition.finalbody[0]))
print(f"PASS: {population} actual acquisition failure/interrupt cases; cmd_run closes in owning finally")
