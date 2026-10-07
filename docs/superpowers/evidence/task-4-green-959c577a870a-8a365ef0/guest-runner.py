import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

root = pathlib.Path(sys.argv[1])
m = json.loads((root / "input.json").read_text())
out = root / "evidence"
out.mkdir(mode=0o700)
head = root / "head"
results = []
active = None
success = False
aborted = False
before = None
after = None
tracked = {}
credential_clean = False
secret = (root / "scanner-credential").read_text()


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def clean(s):
    return re.sub(
        r"(?:nsct_|ghp_|github_pat_|sk-)[A-Za-z0-9_.-]+",
        "[REDACTED]",
        s.replace(secret, "[REDACTED]"),
    )


def environment(name):
    d = root / "isolated" / name
    for p in [d / "home", d / "tmp", d / "data", d / "cache"]:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "PATH": str(root / "tools")
        + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "CADDY_BIN": str(root / "tools/caddy"),
        "HOME": str(d / "home"),
        "TMPDIR": str(d / "tmp"),
        "DATA_DIR": str(d / "data"),
        "XDG_CONFIG_HOME": str(d / "home/config"),
        "XDG_CACHE_HOME": str(d / "cache"),
        "CI": "true",
        "NODE_ENV": "production",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NINEROUTER_TEST_PACKAGES": str(head),
        "npm_config_cache": str(root / "npm-cache"),
        "npm_config_audit": "false",
        "npm_config_fund": "false",
    }


def command(args, cwd=root):
    return subprocess.check_output(
        args, cwd=cwd, env=environment("metadata"), text=True, timeout=30
    ).strip()


def binding():
    b = {
        "head": command(["git", "rev-parse", "HEAD"], head),
        "sourceHashes": {p: digest(head / p) for p in m["sourceHashes"]},
    }
    assert b["head"] == m["head"] and b["sourceHashes"] == m["sourceHashes"], (
        "Guest HEAD or Task 4 source changed"
    )
    for p, h in tracked.items():
        assert digest(head / p) == h, "Guest tracked source changed: " + p
    for p, h in m["dependencyHashes"].items():
        assert digest(head / p) == h, "Guest app/test manifest changed"
    assert digest(head / "tests/package-lock.json") == m["testLockSha256"], (
        "Guest supplementary lock changed"
    )
    return b


def stop_group():
    global active
    if active is None:
        return
    # Signal the owned group even if its leader exited; descendants can outlive it.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(active.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            if active.poll() is None:
                active.send_signal(sig)
            raise RuntimeError("owned guest process group cleanup denied")
        if sig == signal.SIGTERM:
            time.sleep(1)
    active.wait(timeout=5)
    active = None


def interrupt(sig, frame):
    global aborted
    aborted = True
    raise KeyboardInterrupt("guest interrupted or deadline exceeded")


for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGALRM):
    signal.signal(sig, interrupt)
signal.alarm(3600)


def run(name, args, cwd, limit, population, validate=None):
    global active
    if before is not None:
        binding()
    raw = root / (name + ".raw")
    timed = False
    code = None
    cleanup = None
    valid = False
    details = {}
    started = time.monotonic()
    print(json.dumps({"phase": "check-start", "name": name}), flush=True)
    try:
        env = environment(name)
        if name == "secrets":
            env.update(
                SONARQUBE_CLI_TOKEN=secret, SONARQUBE_CLI_ORG="lieberman-family-office"
            )
        if name == "s3776":
            env["PATH"] = str(root / "venv/bin") + ":" + env["PATH"]
        with raw.open("wb") as f:
            active = subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                code = active.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                timed = True
    finally:
        try:
            stop_group()
        except Exception as e:
            cleanup = clean(str(e))
        text = clean(raw.read_text(errors="replace")) if raw.exists() else "NOT RUN"
        (out / (name + ".log")).write_text(text)
        raw.unlink(missing_ok=True)
        try:
            assert code == 0 and not timed and cleanup is None, "failed/unrun command"
            if validate:
                details = validate(text) or {}
            if before is not None:
                binding()
            valid = True
        except Exception as e:
            details = {"validationFailure": clean(str(e))}
        entry = {
            "name": name,
            "args": args,
            "exitCode": code,
            "timeout": timed,
            "cleanupFailure": cleanup,
            "validated": valid,
            "population": population,
            "seconds": time.monotonic() - started,
            **details,
        }
        results.append(entry)
        (out / "checks.json").write_text(json.dumps(results, indent=2))
        print(
            json.dumps(
                {
                    "phase": "check-end",
                    "name": name,
                    "exitCode": code,
                    "validated": valid,
                }
            ),
            flush=True,
        )
    return valid


def unit_summary(text):
    suites = list(ET.parse(out / "pytest.xml").getroot().iter("testsuite"))
    counts = {
        k: sum(int(s.attrib.get(k, 0)) for s in suites)
        for k in ["tests", "failures", "errors", "skipped"]
    }
    (out / "population.json").write_text(json.dumps(counts))
    assert counts["tests"] > 0 and all(
        counts[k] == 0 for k in ["failures", "errors", "skipped"]
    ), "empty/failed/skipped unit population"
    cases = list(ET.parse(out / "pytest.xml").getroot().iter("testcase"))
    assert all(
        any(x.attrib.get("classname", "").endswith(n) for x in cases)
        for n in ["test_9router_hotswap", "test_9router_deploy"]
    ), "unrun unit module"
    return {"testCount": counts["tests"], "moduleCount": 2}


def native_summary(name):
    def validate(text):
        assert not re.search(r"(?i)\b(?:skipped|todo)\b", text), (
            "skipped native assertions"
        )
        markers = {
            "worker": ["PASS: real pinned workers"],
            "proxy": ["PASS"],
            "counterfactual": [
                "GREEN: unchanged byte-identical mirror",
                "RED: removing only",
                "PASS: routing counterfactual",
            ],
        }
        assert all(marker in text for marker in markers[name]), (
            "missing native proof markers"
        )
        return {"requiredMarkers": markers[name]}

    return validate


def secrets_summary(text):
    assert not re.search(
        r"(?i)\b(?:skipped|not entitled|unauthorized|forbidden)\b", text
    ), "secrets leg did not run"
    assert "No secrets found" in text, "secrets scanner did not report a clean result"
    return {"leg": "secrets only; not code analysis"}


try:
    assert command(["uname", "-s"]) == "Darwin" and command(["uname", "-m"]) == "arm64"
    versions = {
        k: command(v)
        for k, v in {
            "node": ["node", "--version"],
            "npm": ["npm", "--version"],
            "python": ["python3", "--version"],
            "macos": ["sw_vers", "-productVersion"],
        }.items()
    }
    (out / "runtime.json").write_text(json.dumps(versions, indent=2))
    assert versions["node"] == "v26.10.0" and versions["npm"] == "11.19.1", (
        "runtime identity mismatch"
    )
    for p, key in [
        ("source.tar", "archiveSha256"),
        ("source.bundle", "bundleSha256"),
        ("runner.py", "runnerSha256"),
        ("sonar_pr_issues.py", "selfCheckSha256"),
        ("gh_owner.py", "ownerHelperSha256"),
        ("tests-package-lock.json", "testLockSha256"),
    ]:
        assert digest(root / p) == m[key], "Transferred artifact hash mismatch: " + p
    head.mkdir(mode=0o700)
    subprocess.run(
        ["/usr/bin/tar", "-xf", str(root / "source.tar"), "-C", str(head)],
        env=environment("extract"),
        check=True,
        timeout=60,
    )
    subprocess.run(
        [
            "git",
            "clone",
            "--no-checkout",
            str(root / "source.bundle"),
            str(root / "git-source"),
        ],
        env=environment("clone"),
        check=True,
        timeout=120,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(root / "git-source"), "checkout", "--detach", m["head"]],
        env=environment("checkout"),
        check=True,
        timeout=60,
        stdout=subprocess.DEVNULL,
    )
    (head / ".git").write_text("gitdir: " + str(root / "git-source/.git") + "\n")
    for p, h in m["sourceHashes"].items():
        incoming = root / pathlib.Path(p).name
        assert digest(incoming) == h
        (head / p).write_bytes(incoming.read_bytes())
    (head / "tests/package-lock.json").write_bytes(
        (root / "tests-package-lock.json").read_bytes()
    )
    for directory in [head, head / "tests"]:
        pkg = json.loads((directory / "package.json").read_text())
        lock = json.loads((directory / "package-lock.json").read_text())
        assert lock["lockfileVersion"] >= 2
        for key in ["dependencies", "devDependencies", "optionalDependencies"]:
            assert pkg.get(key, {}) == lock["packages"][""].get(key, {}), (
                "Lock/manifest mismatch: " + key
            )
    tracked = {
        p: digest(head / p)
        for p in command(["git", "ls-files", "-z"], head).split("\0")
        if p
    }
    before = binding()
    assert run(
        "venv",
        ["python3", "-m", "venv", str(root / "venv")],
        root,
        60,
        {"unit": "guest environments", "count": 1},
    )
    python = str(root / "venv/bin/python")
    assert run(
        "tools",
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "pytest==8.4.2",
            "ruff==0.15.18",
        ],
        root,
        240,
        {"unit": "proven RED tools", "count": 2},
    )
    version_code = (
        "import importlib.metadata as m; assert m"
        '.version("pytest")=="8.4.2"; assert m.ve'
        'rsion("ruff")=="0.15.18"; print("pytest='
        '=8.4.2 ruff==0.15.18")'
    )
    assert run(
        "tool-versions",
        [python, "-c", version_code],
        root,
        30,
        {"unit": "pinned tools", "count": 2},
    )
    for name, cwd in [("app", head), ("tests", head / "tests")]:
        assert run(
            "dependencies-" + name,
            [
                "npm",
                "ci",
                "--ignore-scripts",
                "--include=dev",
                "--include=optional",
                "--no-audit",
                "--no-fund",
            ],
            cwd,
            900,
            {"unit": "hash-bound locked guest installs", "count": 1},
        )
    (out / "dependencies.json").write_text(
        json.dumps(
            {
                "dependencyHashes": m["dependencyHashes"],
                "testLockSha256": m["testLockSha256"],
                "install": "guest-only npm ci --ignore-scripts; no host dependencies",
            },
            indent=2,
        )
    )
    all_ok = run(
        "controller-tests",
        [
            python,
            "-m",
            "pytest",
            "tests/mac/test_9router_hotswap.py",
            "tests/mac/test_9router_deploy.py",
            "-vv",
            "--maxfail=1",
            "-o",
            "faulthandler_timeout=60",
            "--junitxml=" + str(out / "pytest.xml"),
        ],
        head,
        600,
        {"unit": "Task 4 and legacy deploy unit modules", "count": 2},
        unit_summary,
    )
    for name, args in [
        (
            "ruff-check",
            [python, "-m", "ruff", "check", "--select=E,F,I", *m["sourceHashes"]],
        ),
        (
            "ruff-format",
            [python, "-m", "ruff", "format", "--check", *m["sourceHashes"]],
        ),
    ]:
        passed = run(
            name, args, head, 60, {"unit": "exact Task 4 Python files", "count": 4}
        )
        all_ok = passed and all_ok
        if name == "ruff-format" and not passed:
            for p in m["sourceHashes"]:
                formatted = subprocess.run(
                    [python, "-m", "ruff", "format", "--stdin-filename", p],
                    input=(head / p).read_bytes(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment("format-preview"),
                    check=True,
                    timeout=30,
                )
                (out / ("formatted-" + pathlib.Path(p).name)).write_bytes(
                    formatted.stdout
                )
    for i, p in enumerate(m["sourceHashes"]):
        all_ok = (
            run(
                "syntax-" + str(i),
                [python, "-m", "py_compile", p],
                head,
                30,
                {"unit": "Task 4 Python files", "count": 1},
            )
            and all_ok
        )
    # Explicit population remains visible when the helper reports advisory C901 debt.
    code = (
        "import sys;sys.path.insert(0,"
        + repr(str(root))
        + (
            ");import sonar_pr_issues as s; p=sys.arg"
            'v[2:]; assert len(p)==4; print("task4-se'
            'lf-check-population: 4 file(s)",flush=Tr'
            "ue); raise SystemExit(s.self_check(p,bas"
            "e=sys.argv[1]))"
        )
    )

    def selfcheck_summary(text):
        assert re.search(r"^task4-self-check-population: 4 file\(s\)$", text, re.M), (
            "zero/unreadable self-check population"
        )
        assert "self-check" in text and not re.search(
            r"(?i)could not run|0 file\(s\)|could not parse", text
        ), "self-check did not run"
        return {
            "fileCount": 4,
            "leg": "S3776 self-check heuristic; not Sonar code analysis",
        }

    all_ok = (
        run(
            "s3776",
            [python, "-c", code, m["head"], *m["sourceHashes"]],
            head,
            60,
            {"unit": "exact Task 4 Python files", "count": 4},
            selfcheck_summary,
        )
        and all_ok
    )
    caddy = root / "tools/caddy"
    tar = root / "caddy.tgz"
    assert run(
        "caddy-download",
        [
            "curl",
            "--fail",
            "--location",
            "--max-time",
            "120",
            "--output",
            str(tar),
            (
                "https://github.com/caddyserver/caddy/rel"
                "eases/download/v2.11.4/caddy_2.11.4_mac_"
                "arm64.tar.gz"
            ),
        ],
        root,
        150,
        {"unit": "pinned guest runtime downloads", "count": 1},
    )
    assert (
        digest(tar)
        == "9efb0af2d6cf09cfb5053c0e51721b9b3d4956d346234f39368d943d25a3c9a7"
    )
    assert run(
        "caddy-extract",
        ["tar", "-xzf", str(tar), "-C", str(root / "tools"), "caddy"],
        root,
        30,
        {"unit": "hash-verified runtime extractions", "count": 1},
    )
    caddy.chmod(0o700)
    versions["caddy"] = command([str(caddy), "version"])
    assert versions["caddy"].split()[0] == "v2.11.4"
    (out / "runtime.json").write_text(json.dumps(versions, indent=2))
    for name, file in [
        ("worker", "9router_worker.check.cjs"),
        ("proxy", "9router_hotswap.check.cjs"),
        ("counterfactual", "9router_hotswap_counterfactual.check.cjs"),
    ]:
        all_ok = (
            run(
                name,
                ["node", "tests/mac/" + file],
                head,
                600,
                {"unit": "native assertion commands", "count": 1},
                native_summary(name),
            )
            and all_ok
        )
    scanner = root / "tools/sonar"
    assert run(
        "scanner-download",
        [
            "curl",
            "--fail",
            "--location",
            "--max-time",
            "120",
            "--output",
            str(scanner),
            (
                "https://binaries.sonarsource.com/Distrib"
                "ution/sonarqube-cli/1.9.0.15656/macos/so"
                "narqube-cli-1.9.0.15656-macos-arm64.bin"
            ),
        ],
        root,
        150,
        {"unit": "pinned guest scanners", "count": 1},
    )
    assert (
        digest(scanner)
        == "8de8ec62c3614a9abb7053114fda85b9460b6ebdbca7bb612c6e58dc88ab2015"
    )
    scanner.chmod(0o700)
    all_ok = (
        run(
            "scanner-version",
            [str(scanner), "--version"],
            head,
            60,
            {"unit": "pinned scanners", "count": 1},
        )
        and all_ok
    )
    all_ok = (
        run(
            "secrets",
            [str(scanner), "analyze", "secrets", *m["sourceHashes"]],
            head,
            600,
            {"unit": "exact Task 4 source files", "count": 4},
            secrets_summary,
        )
        and all_ok
    )
    names = [x["name"] for x in results]
    assert sorted(names) == sorted(m["requiredChecks"]) and all(
        x["validated"] for x in results
    ), "failed/unrun required checks"
    success = all_ok
except Exception as e:
    success = False
    (out / "failure.json").write_text(
        json.dumps({"type": type(e).__name__, "reason": clean(str(e))})
    )
finally:
    signal.alarm(0)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGALRM):
        signal.signal(sig, signal.SIG_IGN)
    try:
        stop_group()
    except Exception as e:
        success = False
        (out / "cleanup-failure.json").write_text(json.dumps({"reason": clean(str(e))}))
    try:
        (root / "scanner-credential").unlink(missing_ok=True)
        credential_clean = not (root / "scanner-credential").exists()
    except Exception as e:
        success = False
        (out / "credential-failure.json").write_text(
            json.dumps({"reason": clean(str(e))})
        )
    try:
        after = binding()
        assert before == after
    except Exception as e:
        success = False
        (out / "immutability-failure.json").write_text(
            json.dumps({"reason": clean(str(e))})
        )
    success = success and credential_clean and not aborted
    (out / "source-binding.json").write_text(
        json.dumps({"before": before, "after": after}, indent=2)
    )
    (out / "checks.json").write_text(json.dumps(results, indent=2))
    identity = {
        **m,
        "success": success,
        "interrupted": aborted,
        "credentialCleanup": credential_clean,
        "sourceBefore": before,
        "sourceAfter": after,
        "scope": m["scope"],
    }
    (out / "identity.json").write_text(json.dumps(identity, indent=2))
    for p in out.iterdir():
        if p.is_file():
            p.write_text(clean(p.read_text(errors="replace")))
    files = {p.name: digest(p) for p in out.iterdir() if p.is_file()}
    pending = out / "manifest.pending"
    pending.write_text(
        json.dumps(
            {
                "files": files,
                "head": m["head"],
                "instanceId": m["instanceId"],
                "runId": m["runId"],
            }
        )
    )
    pending.replace(out / "manifest.json")
print(json.dumps({"phase": "evidence-complete", "success": success}), flush=True)
sys.exit(0 if success else 2)
