#!/usr/bin/env python3
"""Run changed-file push checks only in Namespace and export exact outcomes."""

import ast
import hashlib
import importlib.util
import io
import os
import subprocess
import sys
import tokenize
import urllib.request
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
q.runtime_preflight(binding, run)
out = run / "evidence"
out.mkdir(mode=0o700)
checks = []
secret = run / "scanner-credential"


def wrap_long_literals(filename):
    path = source / filename
    original = path.read_text()
    lines = original.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    replacements = []
    tokens = list(tokenize.generate_tokens(io.StringIO(original).readline))
    significant = [
        item for item in tokens if item.type not in {tokenize.NL, tokenize.COMMENT}
    ]
    for token_index, token in enumerate(significant):
        if token.type != tokenize.STRING or not any(
            len(line.rstrip("\n")) > 88
            for line in lines[token.start[0] - 1 : token.end[0]]
        ):
            continue
        try:
            value = ast.literal_eval(token.string)
        except (ValueError, SyntaxError):
            continue
        if not isinstance(value, str) or not value:
            continue
        indent = " " * (len(token.line) - len(token.line.lstrip()))
        chunks = [repr(value[index : index + 40]) for index in range(0, len(value), 40)]
        adjacent = any(
            0 <= index < len(significant) and significant[index].type == tokenize.STRING
            for index in (token_index - 1, token_index + 1)
        )
        replacement = (
            (("\n") + indent + ("    ")).join(chunks)
            if adjacent
            else ("(\n")
            + "".join(indent + ("    ") + chunk + ("\n") for chunk in chunks)
            + indent
            + (")")
        )
        start = offsets[token.start[0] - 1] + token.start[1]
        end = offsets[token.end[0] - 1] + token.end[1]
        replacements.append((start, end, replacement))
    updated = original
    for start, end, replacement in reversed(replacements):
        updated = updated[:start] + replacement + updated[end:]
    q.require(
        ast.dump(ast.parse(original)) == ast.dump(ast.parse(updated)),
        ("Literal wrapping changed Python semantics: ") + filename,
    )
    path.write_text(updated)


def check(name, argv, env, subjects, timeout=180):
    result = subprocess.run(
        argv, cwd=source, env=env, capture_output=True, text=True, timeout=timeout
    )
    log = result.stdout + result.stderr
    if secret.exists():
        log = log.replace(secret.read_text(), "[REDACTED]")
    (out / (name + ".log")).write_text(log)
    checks.append(
        {
            "name": name,
            "exit_code": result.returncode,
            "subjects": subjects,
            "command": argv,
            "log_sha256": q.sha256(out / (name + ".log")),
        }
    )
    return result.returncode


try:
    env = q.isolated_environment(run / "checks-home", source)
    env.pop("NODE_ENV")
    for folder in [source, source / "tests", source / "cli"]:
        lock = folder / "package-lock.json"
        if not lock.exists():
            lock.write_bytes((source / q.TEST_LOCK).read_bytes())
        q.guest_command(
            [
                "npm",
                "ci",
                "--ignore-scripts",
                "--include=dev",
                "--include=optional",
                "--no-audit",
                "--no-fund",
            ],
            folder,
            env,
            900,
        )
    venv = run / "tools"
    q.output(["python3", "-m", "venv", str(venv)], env=env)
    python = str(venv / "bin/python")
    q.guest_command(
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "ruff==0.15.18",
        ],
        source,
        env,
        180,
    )
    changed = q.output(
        ["git", "diff", "--name-only", "c0ceebc70d892398870f93dba9af522f870ccc08"],
        source,
    ).splitlines()
    python_files = [name for name in changed if name.endswith(".py")]
    js_files = [
        name for name in changed if Path(name).suffix in {".js", ".cjs", ".mjs"}
    ]
    q.require(python_files and js_files and changed, "Zero changed check population")
    for filename in python_files:
        wrap_long_literals(filename)
    q.new_json(
        out / ("pre-check-formatting.json"),
        {
            ("files"): {
                filename: q.sha256(source / filename) for filename in python_files
            }
        },
    )
    q.guest_command(
        [python, "-m", "ruff", "check", "--select=F401,I", "--fix", *python_files],
        source,
        env,
        180,
    )
    q.guest_command([python, "-m", "ruff", "format", *python_files], source, env, 180)
    check(
        "ruff-check",
        [python, "-m", "ruff", "check", "--select=E,F,I", *python_files],
        env,
        len(python_files),
    )
    check(
        "ruff-format",
        [python, "-m", "ruff", "format", "--check", *python_files],
        env,
        len(python_files),
    )
    check(
        "eslint",
        ["node", "node_modules/eslint/bin/eslint.js", "--no-warn-ignored", *js_files],
        env,
        len(js_files),
    )
    for name in js_files:
        check(
            "syntax-" + hashlib.sha256(name.encode()).hexdigest()[:8],
            ["node", "--check", name],
            env,
            1,
        )
    selfcheck = (
        "import sys; sys.path.insert(0, sys.argv["
        "1]); import sonar_pr_issues as s; raise "
        "SystemExit(s.self_check(sys.argv[3:], ba"
        "se=sys.argv[2]))"
    )
    self_env = {**env, "PATH": str(venv / "bin") + os.pathsep + env["PATH"]}
    check(
        "s3776-self-check",
        [
            python,
            "-c",
            selfcheck,
            str(run),
            "c0ceebc70d892398870f93dba9af522f870ccc08",
            *python_files,
        ],
        self_env,
        len(python_files),
    )
    check(
        ("qualifier-regressions"),
        [("python3"), ("tests/mac/test_9router_vm_qualify.py")],
        env,
        1,
        300,
    )
    check(
        ("checkpoint-regressions"),
        [("python3"), ("tests/mac/9router_test_credentials.check.py")],
        env,
        1,
        180,
    )
    check(
        "claim-cleanup-regressions",
        ["python3", "tests/mac/task5_claim_cleanup.check.py"],
        env,
        29,
        180,
    )
    # Produce only a guest formatting/import diff for the host authoring surface.
    q.guest_command(
        [python, "-m", "ruff", "check", "--select=F401,I", "--fix", *python_files],
        source,
        env,
        180,
    )
    q.guest_command([python, "-m", "ruff", "format", *python_files], source, env, 180)
    patch = subprocess.run(
        ["git", "diff", "--binary"], cwd=source, capture_output=True, check=True
    ).stdout
    (out / "format.patch").write_bytes(patch)
    if secret.exists():
        scanner = run / "sonar"
        url = (
            "https://binaries.sonarsource.com/Distrib"
            "ution/sonarqube-cli/1.9.0.15656/macos/so"
            "narqube-cli-1.9.0.15656-macos-arm64.bin"
        )
        with (
            urllib.request.urlopen(url, timeout=120) as incoming,
            scanner.open("xb") as stream,
        ):
            __import__("shutil").copyfileobj(incoming, stream)
        q.require(
            q.sha256(scanner)
            == "8de8ec62c3614a9abb7053114fda85b9460b6ebdbca7bb612c6e58dc88ab2015",
            "Scanner pin mismatch",
        )
        scanner.chmod(0o700)
        env.update(
            SONARQUBE_CLI_TOKEN=secret.read_text(),
            SONARQUBE_CLI_ORG="lieberman-family-office",
        )
        history = run / "historical-blobs"
        history.mkdir(mode=0o700)
        commits = q.output(
            [
                ("git"),
                ("rev-list"),
                ("HEAD"),
                ("^c0ceebc70d892398870f93dba9af522f870ccc08"),
            ],
            source,
        ).splitlines()
        objects = {}
        for commit in commits:
            for entry in q.output(
                [("git"), ("ls-tree"), ("-r"), ("-z"), commit], source
            ).split(("\x00")):
                if not entry:
                    continue
                metadata, filename = entry.split("\t", 1)
                mode, kind, oid = metadata.split()
                if kind == "blob":
                    objects.setdefault(oid, set()).add(filename)
        base_objects = {
            entry.split("\t", 1)[0].split()[2]
            for entry in q.output(
                [
                    ("git"),
                    ("ls-tree"),
                    ("-r"),
                    ("-z"),
                    ("c0ceebc70d892398870f93dba9af522f870ccc08"),
                ],
                source,
            ).split(("\x00"))
            if entry
        }
        historical_paths = []
        census = {}
        for oid, filenames in sorted(objects.items()):
            if oid in base_objects:
                continue
            target = history / oid
            result = subprocess.run(
                [("git"), ("cat-file"), ("blob"), oid],
                cwd=source,
                capture_output=True,
                check=True,
            )
            target.write_bytes(result.stdout)
            census[oid] = {
                ("paths"): sorted(filenames),
                ("sha256"): q.sha256(target),
                ("bytes"): len(result.stdout),
            }
            historical_paths.append(str(target))
        q.require(
            commits and historical_paths, ("Zero historical publication population")
        )
        q.new_json(
            out / ("historical-blob-census.json"),
            {
                ("commits"): len(commits),
                ("unique_blobs"): len(census),
                ("objects"): census,
            },
        )
        for offset in range(0, len(historical_paths), 10):
            population = historical_paths[offset : offset + 10]
            check(
                ("historical-secrets-") + str(offset),
                [str(scanner), ("analyze"), ("secrets"), *population],
                env,
                len(population),
                600,
            )
        for offset in range(0, len(changed), 10):
            population = changed[offset : offset + 10]
            check(
                "sonar-secrets-" + str(offset),
                [str(scanner), "analyze", "secrets", *population],
                env,
                len(population),
                600,
            )
    else:
        checks.append(
            {
                "name": "sonar-secrets",
                "exit_code": None,
                "subjects": len(changed),
                "failure": "Existing scanner credential missing",
            }
        )
except BaseException as error:
    q.new_json(
        out / ("runner-failure.json"),
        {
            ("failure"): type(error).__name__,
            ("reason"): str(error)
            if isinstance(error, ValueError)
            else ("Guest setup failed"),
        },
    )
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise
finally:
    secret.unlink(missing_ok=True)
    q.new_json(
        out / "push-checks.json",
        {
            "source_commit": binding["source_commit"],
            "namespace": binding["namespace"],
            "checks": checks,
            "success": bool(checks)
            and not (out / "runner-failure.json").exists()
            and all(
                item.get("exit_code") == 0 and item["subjects"] > 0 for item in checks
            ),
        },
    )
    q.new_json(
        out / "export.json",
        {
            "files": {
                p.name: q.sha256(p)
                for p in out.iterdir()
                if p.is_file() and p.stat().st_size > 0
            }
        },
    )
