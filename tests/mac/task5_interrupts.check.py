#!/usr/bin/env python3
"""Exercise four real guest dispatches with fictional state and injected faults."""

import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/superpowers/evidence/task5-continuity-recovery-d6ba92d8"
FILES = (
    "guest-enrollment-diagnostic.py",
    "guest-push-checks.py",
    "guest-reproduce.py",
    "guest-signin.py",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(filename, error, out, checks, results, secret, calls, propagated):
    assert calls.count("fault") == 1, (filename, "wrong fault dispatch", calls)
    if filename == "guest-push-checks.py":
        assert len(checks) == 1 and checks[0]["exit_code"] == 0
        assert not secret.exists(), "scanner cleanup did not run"
        assert json.loads((out / "push-checks.json").read_text())["success"] is False, (
            filename,
            "interrupted partial checks reported success",
        )
    failure_name = {
        "guest-enrollment-diagnostic.py": "enrollment-diagnostic.json",
        "guest-push-checks.py": "runner-failure.json",
        "guest-reproduce.py": "guest-reproduction.json",
        "guest-signin.py": "failure.json",
    }[filename]
    assert (out / failure_name).exists(), (filename, "failure evidence missing")
    failure = json.loads((out / failure_name).read_text())
    observed = failure.get("failure", failure.get("type"))
    if isinstance(observed, dict):
        observed = observed["type"]
    assert observed == type(error).__name__, (filename, "wrong failure", failure)
    export_name = "manifest.json" if filename == FILES[0] else "export.json"
    exported = json.loads((out / export_name).read_text())["files"]
    assert exported[failure_name] == digest(out / failure_name)
    assert all(digest(out / name) == value for name, value in exported.items())
    if filename == "guest-reproduce.py":
        assert len(results) == 2 and results[1]["failure"] == type(error).__name__
        assert failure["success"] is False
    elif filename == FILES[0]:
        assert failure["enrolled"] is False
        assert json.loads((out / "guest.json").read_text())["guest_result"] == "fail"
    elif filename == "guest-signin.py":
        assert not (out / "checkpoint.json").exists()
    if isinstance(error, RuntimeError):
        assert propagated is None, (filename, "ordinary error contract changed")
    else:
        assert propagated is error, (filename, "interrupt swallowed", propagated)
        if isinstance(error, SystemExit):
            assert propagated.code == error.code


def dispatch(program, filename, namespace):
    try:
        exec(compile(program, str(EVIDENCE / filename), "exec"), namespace)
    except (KeyboardInterrupt, SystemExit) as caught:
        return caught
    return None


def guest_output(filename, checked_command, *_args, **_kwargs):
    if filename == "guest-reproduce.py":
        return checked_command().stdout
    return "fictional\n"


def exercise(filename, error):
    tree = ast.parse((EVIDENCE / filename).read_text())
    outer = next(node for node in tree.body if isinstance(node, ast.Try))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    with tempfile.TemporaryDirectory() as folder:
        run = Path(folder)
        source, out = run / "source", run / "evidence"
        out.mkdir()
        for name in ("", "tests", "cli"):
            directory = source / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "package.json").write_text("{}")
            (directory / "package-lock.json").write_text('{"packages":{"":{}}}')
        (source / "subject.py").write_text("value = 1\n")
        (source / "qualification.patch").write_text("")
        secret = run / "scanner-credential"
        secret.write_text("fictional")
        calls = []
        bindings = {"source_commit": "fictional", "namespace": {"id": "fictional"}}
        bindings.update(
            source_files={}, source_patch_sha256=digest(source / "qualification.patch")
        )
        results, checks = [], []
        record = {"success": False, "guest_result": "fail", "checks": results}

        def fail(*_args, **_kwargs):
            calls.append("fault")
            raise error

        def checked_command(*_args, **_kwargs):
            calls.append("check")
            if calls.count("check") == 2:
                fail()
            return SimpleNamespace(returncode=0, stdout="fictional\n", stderr="")

        def guest_command(*_args, **_kwargs):
            return guest_output(filename, checked_command)

        def output(argv, *_args, **_kwargs):
            if "diff" in argv:
                return "subject.py\nsubject.js"
            return "fictional"

        def require(condition, message):
            if not condition:
                raise ValueError(message)

        def new_json(path, value):
            path.write_text(json.dumps(value))

        q = SimpleNamespace(
            isolated_environment=lambda *_args: {
                "NODE_ENV": "test",
                "PATH": "fictional",
            },
            provision_dependencies=fail
            if filename == "guest-signin.py"
            else lambda *_args: None,
            checkpoint_baseline=fail,
            runtime_preflight=lambda *_args: None,
            guest_command=guest_command,
            output=output,
            require=require,
            new_json=new_json,
            sha256=digest,
            private_json=lambda path: json.loads(path.read_text()),
            BASELINE=run / "absent-baseline",
            BINDINGS=("source_commit", "namespace"),
        )
        namespace = {
            "q": q,
            "run": run,
            "source": source,
            "evidence": out,
            "out": out,
            "binding": bindings,
            "checks": checks,
            "results": results,
            "record": record,
            "os": os,
            "secret": secret,
            "redactions": [],
            "Path": Path,
            "json": json,
            "ast": ast,
            "hashlib": hashlib,
            "io": io,
            "tokenize": tokenize,
            "subprocess": SimpleNamespace(
                run=checked_command, CalledProcessError=subprocess.CalledProcessError
            ),
        }
        program = ast.fix_missing_locations(
            ast.Module(body=[*functions, outer], type_ignores=[])
        )
        propagated = dispatch(program, filename, namespace)
        verify(filename, error, out, checks, results, secret, calls, propagated)


failures = []
population = 0
for filename in FILES:
    for error in (
        RuntimeError("fictional"),
        KeyboardInterrupt(),
        SystemExit(17),
        SystemExit(0),
    ):
        population += 1
        try:
            exercise(filename, error)
        except AssertionError as failure:
            failures.append((filename, type(error).__name__, str(failure)))
assert not failures, failures
print(
    f"PASS: {population} real guest dispatch cases; "
    "failure/export retained, interrupts propagated"
)
