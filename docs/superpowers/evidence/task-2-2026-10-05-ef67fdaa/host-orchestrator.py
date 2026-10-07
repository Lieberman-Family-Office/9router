# Archived runner cleanup; original executed bytes are in commit 4d24627f.
import datetime
import hashlib
import json
import os
import signal
import subprocess
import tarfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[4]
SCRATCH = Path("/tmp/task2-export-ef67fdaa-20261005-2215")
SCRATCH.mkdir(exist_ok=False)
EVIDENCE = SOURCE / "docs/superpowers/evidence/task-2-2026-10-05-ef67fdaa"
EVIDENCE.mkdir(parents=True, exist_ok=False)
REMOTE = "/Volumes/devbox/task2-ef67fdaa-20261005-2215"
BOX = "9router-qualify-recovery"
HEAD = "05b606931b824cf77dc3cc1164b75df88590ed24"
BASE = "5fd82262218f5f00b4f987587318908e548ad65a"
META = "/tmp/namespace-runtime-check-ef67fdaa/task2-metadata.mjs"
records = []


def run(name, args, timeout=120, cwd=SOURCE):
    path = EVIDENCE / (name + ".log")
    print(name, flush=True)
    with path.open("wb") as f:
        f.write(("COMMAND: " + json.dumps(args) + "\n").encode())
        f.flush()
        p = subprocess.Popen(
            args, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            rc = 124
    r = {
        "name": name,
        "command": args,
        "exit": rc,
        "log": str(path.relative_to(SOURCE)),
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    records.append(r)
    (EVIDENCE / "orchestration.json").write_text(json.dumps(records, indent=2))
    print(json.dumps(r), flush=True)
    return rc


try:
    if run("metadata-before", ["node", META]):
        raise RuntimeError("metadata failed")
    if (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True
        ).strip()
        != HEAD
    ):
        raise RuntimeError("HEAD changed")
    if subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=SOURCE, text=True
    ).strip():
        raise RuntimeError("tracked source dirty")
    for name, commit in [("head", HEAD), ("baseline", BASE)]:
        if run(
            "archive-" + name,
            [
                "git",
                "archive",
                "--format=tar",
                "--output=" + str(SCRATCH / (name + ".tar")),
                commit,
            ],
        ):
            raise RuntimeError("archive failed")
    if run(
        "bundle",
        ["git", "bundle", "create", str(SCRATCH / "source.bundle"), "HEAD", BASE],
    ):
        raise RuntimeError("bundle failed")
    (SCRATCH / "vitest.config.mjs").write_bytes(
        (SOURCE / ".superpowers/sdd/task-2-vitest.config.mjs").read_bytes()
    )
    manifest = {
        "head": HEAD,
        "baseline": BASE,
        "operatorQuote": (
            "Yes—authorize Namespace startup and test"
            "ing, with verified shutdown afterward (R"
            "ecommended)"
        ),
        "operatorTurn": (
            "2026-10-05 17:56 EDT; actual user record"
            "s at transcript lines 3353 and 3355"
        ),
        "devboxId": "2tc0b2eg4mveo",
        "devboxName": BOX,
        "guestRoot": REMOTE,
        "inputs": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in SCRATCH.iterdir()
            if p.is_file()
        },
    }
    (EVIDENCE / "source-manifest.json").write_text(json.dumps(manifest, indent=2))
    (SCRATCH / "expected-inputs.json").write_text(json.dumps(manifest, indent=2))
    if run(
        "guest-isolated-directory",
        ["devbox", "exec", BOX, "--", "/bin/mkdir", REMOTE],
        120,
    ):
        raise RuntimeError("guest isolated mkdir failed")
    for p in SCRATCH.iterdir():
        if p.is_file() and run(
            "upload-" + p.name,
            ["devbox", "upload", BOX, str(p), REMOTE + "/" + p.name],
            240,
        ):
            raise RuntimeError("upload failed " + p.name)
    if run(
        "upload-guest-runner",
        [
            "devbox",
            "upload",
            BOX,
            "/tmp/task2-guest-ef67fdaa.py",
            REMOTE + "/runner.py",
        ],
        120,
    ):
        raise RuntimeError("runner upload failed")
    run("metadata-running", ["node", META])
    run(
        "guest-battery",
        [
            "devbox",
            "exec",
            BOX,
            "--",
            "/opt/homebrew/bin/python3",
            REMOTE + "/runner.py",
        ],
        3000,
    )
except Exception as e:
    (EVIDENCE / "orchestration-error.txt").write_text(repr(e))
    print(repr(e), flush=True)
finally:
    try:
        rc = run(
            "export-evidence",
            [
                "devbox",
                "download",
                BOX,
                REMOTE + "/evidence.tar",
                str(SCRATCH / "evidence.tar"),
            ],
            180,
        )
        if rc == 0:
            with tarfile.open(SCRATCH / "evidence.tar") as t:
                t.extractall(EVIDENCE, filter="data")
            (EVIDENCE / "export-sha256.txt").write_text(
                hashlib.sha256((SCRATCH / "evidence.tar").read_bytes()).hexdigest()
                + "\n"
            )
    except Exception as e:
        (EVIDENCE / "export-error.txt").write_text(repr(e))
    finally:
        # Owned CLI processes were synchronously waited or group-closed.
        run("stop-cli", ["devbox", "stop", BOX, "--force"], 120)
        run("metadata-immediate", ["node", META, "verify"])
        print("CLEANUP_INITIAL_OBSERVATION_COMPLETE", flush=True)
