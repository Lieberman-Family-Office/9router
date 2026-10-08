#!/usr/bin/env python3
"""Run only private checkpoint enrollment in Namespace and export safe diagnostics."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

run = Path(sys.argv[1])
source = run / "source"
spec = importlib.util.spec_from_file_location(
    "qualifier", source / "scripts/mac/9router_vm_qualify.py"
)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
q.guest_guard()
os.umask(0o077)
binding = q.private_json(run / "input.json")
q.source_binding(source, binding)
q.runtime_preflight(binding, run)
evidence = run / "evidence"
evidence.mkdir(mode=0o700)
record = {**binding, "guest_result": "fail", "result": "diagnostic-only"}
redactions = []
values = q.credential_module().environment(run / "credential-input/env.sh")
redactions.extend(
    value
    for key, value in values.items()
    if key.endswith(("SECRET")) or key == ("INITIAL_PASSWORD")
)
with sqlite3.connect(
    (run / ("credential-input/app.sqlite")).as_uri() + ("?mode=ro"), uri=True
) as database:
    for (raw,) in database.execute("SELECT data FROM providerConnections"):
        account = json.loads(raw)
        redactions.extend(
            value
            for key, value in account.items()
            if isinstance(value, str)
            and any(word in key.lower() for word in (("token"), ("key"), ("secret")))
        )
    redactions.extend(key for (key,) in database.execute("SELECT key FROM apiKeys"))


def sanitize(text):
    for value in sorted(set(redactions), key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


try:
    q.provision_dependencies(binding, run)
    q.checkpoint_baseline(run / "candidate.tgz", binding, run)
    q.new_json(
        evidence / ("enrollment-diagnostic.json"),
        {
            ("enrolled"): True,
            ("scope"): ("checkpoint enrollment only; authentication streams not run"),
        },
    )
except subprocess.CalledProcessError as error:
    stderr = (
        error.stderr.decode() if isinstance(error.stderr, bytes) else error.stderr or ""
    )
    stdout = (
        error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout or ""
    )
    q.new_json(
        evidence / "enrollment-diagnostic.json",
        {
            "enrolled": False,
            "exit_code": error.returncode,
            "stderr": sanitize(stderr),
            "stdout": sanitize(stdout),
            "scope": (
                "exact controller enrollment dispatch only; no authentication verdict"
            ),
        },
    )
except BaseException as error:
    q.new_json(
        evidence / ("enrollment-diagnostic.json"),
        {
            ("enrolled"): False,
            ("failure"): type(error).__name__,
            ("reason"): sanitize(str(error))
            if isinstance(error, ValueError)
            else ("Guest diagnostic failed"),
        },
    )
finally:
    locator = run / "credential-scope.json"
    if locator.exists():
        home = Path(q.private_json(locator)["home"])
        state = home / ".9router/hotswap/state.json"
        if state.exists():
            journal = q.private_json(state)
            q.new_json(
                evidence / "transaction-diagnostic.json",
                {
                    ("enrollment"): journal.get(("enrollment")),
                    ("active"): journal.get(("active")),
                    ("slots"): {
                        key: {
                            field: value.get(field)
                            for field in (("mode"), ("version"), ("digest"))
                        }
                        if value
                        else None
                        for key, value in journal.get(("slots"), {}).items()
                    },
                },
            )
        logs = {}
        for filename in (
            ("a.stdout.log"),
            ("a.stderr.log"),
            ("proxy.stdout.log"),
            ("proxy.stderr.log"),
        ):
            path = home / ".9router/hotswap" / filename
            if path.is_file():
                logs[filename] = {
                    ("sha256"): q.sha256(path),
                    ("lines"): [
                        sanitize(line)
                        for line in path.read_text(errors=("replace")).splitlines()
                        if any(
                            word in line.lower()
                            for word in (
                                ("refused"),
                                ("failed"),
                                ("error"),
                                ("ready"),
                                ("listening"),
                            )
                        )
                    ][-30:],
                }
        q.new_json(evidence / "worker-diagnostic.json", logs)
    q.new_json(
        evidence / ("identity.json"), {key: binding.get(key) for key in q.BINDINGS}
    )
    q.new_json(evidence / "checks.json", {})
    q.new_json(evidence / "guest.json", record)
    q.new_json(
        evidence / ("manifest.json"),
        {("files"): {path.name: q.sha256(path) for path in evidence.iterdir()}},
    )
