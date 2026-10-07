# Archived runner cleanup; original executed bytes are in commit 4d24627f.
import hashlib
import json
import os
import pathlib
import platform
import signal
import subprocess
import sys
import time

root = pathlib.Path(sys.argv[1])
m = json.loads((root / "input.json").read_text())
out = root / "evidence"
out.mkdir(mode=0o700)
checks = []
active = None
complete = False


def digest(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def save(p, v):
    p.write_text(json.dumps(v, indent=2))


def environment(label):
    d = root / "isolated" / label
    for n in ("home", "data", "tmp", "cache"):
        (d / n).mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "PATH": str(root / "tools")
        + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(d / "home"),
        "DATA_DIR": str(d / "data"),
        "TMPDIR": str(d / "tmp") + "/",
        "XDG_CACHE_HOME": str(d / "cache"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "CI": "true",
    }


def stop_owned():
    global active
    if active is not None and active.poll() is None:
        try:
            os.killpg(active.pid, signal.SIGTERM)
        except PermissionError:
            active.terminate()
        try:
            active.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(active.pid, signal.SIGKILL)
            except PermissionError:
                active.kill()
            active.wait(timeout=5)
    active = None


def run(label, args, cwd, timeout=120, extra=None):
    global active
    start = time.time()
    env = environment(label)
    env.update(extra or {})
    log = out / (label + ".log")
    rc = None
    timed = False
    try:
        with log.open("wb") as f:
            active = subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                rc = active.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed = True
    finally:
        stop_owned()
        entry = {
            "name": label,
            "argv": args,
            "exitCode": rc,
            "timeout": timed,
            "startedAt": start,
            "finishedAt": time.time(),
            "logSha256": digest(log),
        }
        checks.append(entry)
        save(out / "checks.json", checks)
    print(json.dumps(entry), flush=True)
    return rc


try:
    assert platform.system() == "Darwin" and platform.machine() == "arm64"
    assert digest(root / "source.tar") == m["sourceArchiveSha256"]
    assert digest(root / "runner.py") == m["guestRunnerSha256"]
    assert digest(root / "checkpoint_plugin.py") == m["pluginSha256"]
    checkout = root / "source"
    checkout.mkdir(mode=0o700)
    (root / "tools").mkdir(mode=0o700)
    assert (
        run(
            "extract",
            ["/usr/bin/tar", "-xf", str(root / "source.tar"), "-C", str(checkout)],
            root,
        )
        == 0
    )
    for p, h in m["overlay"].items():
        incoming = root / pathlib.Path(p).name
        assert digest(incoming) == h
        target = checkout / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(incoming.read_bytes())
        assert digest(target) == h
    assert not (checkout / "scripts/mac/9router_hotswap.py").exists(), (
        "Controller exists: checkpoint is not the reviewed RED state"
    )
    candidates = []
    for p in [
        sys.executable,
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]:
        if p not in candidates and pathlib.Path(p).exists():
            candidates.append(p)
    selected = None
    for i, p in enumerate(candidates):
        if (
            run(
                "pytest-probe-" + str(i),
                [p, "-c", "import pytest; print(pytest.__version__)"],
                root,
            )
            == 0
        ):
            selected = p
            break
    installed = False
    if selected is None:
        assert (
            run("venv", [sys.executable, "-m", "venv", str(root / "venv")], root) == 0
        )
        selected = str(root / "venv/bin/python3")
        assert (
            run(
                "pytest-install",
                [
                    selected,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "pytest==8.4.2",
                ],
                root,
                300,
            )
            == 0
        )
        installed = True
    runtime = {
        "devboxId": m["devboxId"],
        "instanceId": m["instanceId"],
        "os": platform.platform(),
        "machine": platform.machine(),
        "bootstrapPython": sys.version,
        "pythonExecutable": selected,
        "pythonSha256": digest(pathlib.Path(selected).resolve()),
        "installedPytest": installed,
    }
    assert (
        run(
            "runtime",
            [
                selected,
                "-c",
                (
                    "import sys,pytest,importlib.metadata,jso"
                    'n;print(json.dumps({"python":sys.version'
                    ',"pytest":pytest.__version__,"packages":'
                    '{d.metadata["Name"]:d.version for d in i'
                    "mportlib.metadata.distributions()}},inde"
                    "nt=2))"
                ),
            ],
            root,
        )
        == 0
    )
    runtime["versionLogSha256"] = digest(out / "runtime.log")
    save(out / "runtime.json", runtime)
    for p in m["overlay"]:
        label = pathlib.Path(p).stem
        report = out / (label + ".json")
        rc = run(
            label,
            [selected, "-m", "pytest", p, "-q"],
            checkout,
            180,
            {
                "PYTHONPATH": str(root),
                "PYTEST_PLUGINS": "checkpoint_plugin",
                "CHECK_REPORT": str(report),
            },
        )
        assert report.exists(), "pytest did not produce report"
        parsed = json.loads(report.read_text())
        assert parsed["exitCode"] == rc
        assert parsed["collected"] and not parsed["collectionFailures"], (
            "Collection failed or zero tests"
        )
        assert rc == 1, (
            "Expected actual pytest RED (exit 1), not"
            " infrastructure or unexpected GREEN"
        )
        assert not any(
            "ModuleNotFoundError" in (r["message"] or "") for r in parsed["reports"]
        ), "Dependency/import failure"
        failed = [r for r in parsed["reports"] if r["outcome"] == "failed"]
        passed = [
            r["nodeid"]
            for r in parsed["reports"]
            if r["phase"] == "call" and r["outcome"] == "passed"
        ]
        skipped = [r["nodeid"] for r in parsed["reports"] if r["outcome"] == "skipped"]
        checks[-1].update(
            collected=len(parsed["collected"]),
            passed=len(passed),
            failed=sum(r["phase"] == "call" for r in failed),
            errors=sum(r["phase"] != "call" for r in failed),
            skipped=len(skipped),
            failedIdentities=[r["nodeid"] for r in failed],
            reportSha256=digest(report),
        )
        if label == "test_9router_hotswap":
            assert failed and all(
                r["phase"] == "setup" and "Task 4 controller is missing" in r["message"]
                for r in failed
            ), "Hotswap RED is not uniformly the missing controller assertion"
            checks[-1]["cause"] = (
                "Task 4 controller is missing; fixture assertion before import"
            )
        else:
            checks[-1]["cause"] = (
                "Task 4 deploy contracts; inspect individually bound report and log"
            )
        save(out / "checks.json", checks)
    for p, h in m["overlay"].items():
        assert digest(checkout / p) == h, "Test overlay mutated"
    complete = True
except BaseException as e:
    save(out / "failure.json", {"type": type(e).__name__, "message": str(e)})
finally:
    stop_owned()
    save(
        out / "identity.json",
        {
            "head": m["head"],
            "sourceArchiveSha256": m["sourceArchiveSha256"],
            "overlay": m["overlay"],
            "guestRunnerSha256": m["guestRunnerSha256"],
            "pluginSha256": m["pluginSha256"],
            "devboxId": m["devboxId"],
            "instanceId": m["instanceId"],
            "completedRedRuns": complete,
            "scope": (
                "Task 4 RED only; no controller implement"
                "ation, lint, scans, or production qualif"
                "ication"
            ),
        },
    )
    files = {p.name: digest(p) for p in out.iterdir() if p.is_file()}
    save(
        out / "manifest.pending",
        {"head": m["head"], "instanceId": m["instanceId"], "files": files},
    )
    os.replace(out / "manifest.pending", out / "manifest.json")
print(json.dumps({"completedRedRuns": complete}), flush=True)
sys.exit(0 if complete else 2)
