#!/usr/bin/env python3
"""Stage changed-file checks in one owned Namespace session; never test locally."""

import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

source = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "qualifier", source / "scripts/mac/9router_vm_qualify.py"
)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
identity = "2tc0b2eg4mveo"
name = "9router-qualify-recovery"
quote = (
    "guest lint/secrets scans, repair PR review and merge - APPROVED. PUSH AND OPEN PR"
)
run_id = "task5-land-checks-" + uuid.uuid4().hex[:12]
remote = q.GUEST_ROOT / run_id
local = Path(__file__).parent / run_id
local.mkdir(mode=0o700)
stage = Path(tempfile.mkdtemp(prefix="task5-land-stage-"))
sdk = Path("/private/tmp/namespace-runtime-check-ef67fdaa")
cli = str(Path.home() / ".local/bin/devbox")
env = {
    k: v
    for k, v in os.environ.items()
    if k not in {"SONAR_TOKEN", "SONARQUBE_CLI_TOKEN", "NINEROUTER_PROBE_KEY"}
}
env["PYTHONDONTWRITEBYTECODE"] = "1"
claims = []
observations = []
secret = stage / "scanner-credential"


def observe(_identity):
    value = q.host_metadata(identity, sdk)
    observations.append({"at": time.time(), **value})
    return value


def command(*argv, timeout=180):
    for fd, path in claims:
        held, live = os.fstat(fd), path.lstat()
        q.require(
            (held.st_dev, held.st_ino) == (live.st_dev, live.st_ino),
            "Lifecycle claim changed",
        )
    return q.output([cli, *argv], source, env, timeout)


def work():
    command("exec", name, "--", "/usr/bin/uname", "-m")
    active = observe(identity)
    q.require(
        active["state"] == "running" and active["instance_id"],
        "Activation identity unproven",
    )
    command("exec", name, "--", "/bin/mkdir", "-p", "-m", "700", str(remote))
    program = Path(__file__).parent / "guest-push-checks.py"
    command("upload", name, str(program), str(remote / "guest-push-checks.py"))
    command(
        "upload",
        name,
        str(q.HERE / "9router_vm_qualify.py"),
        str(remote / "runtime.py"),
    )
    binaries = json.loads(
        command(
            "exec",
            name,
            "--",
            "python3",
            str(remote / "runtime.py"),
            "runtime",
            "--tools",
            str(q.GUEST_ROOT / "tools" / q.CADDY_ARCHIVE_SHA256),
        )
    )
    commit = q.output(["git", "rev-parse", "HEAD"], source)
    patch = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=source, capture_output=True, check=True
    ).stdout
    patch_path = stage / "source.patch"
    patch_path.write_bytes(patch)
    bundle = stage / "source.bundle"
    q.output(["git", "bundle", "create", str(bundle), "HEAD"], source, timeout=180)
    tracked = q.output(["git", "ls-files", "-z"], source).split("\0")
    binding = {
        "source_commit": commit,
        "source_patch_sha256": hashlib.sha256(patch).hexdigest(),
        "source_files": {p: q.sha256(source / p) for p in tracked if p},
        "bundle_sha256": q.sha256(bundle),
        "guest_program_sha256": q.sha256(program),
        "source_runtime_sha256": q.source_runtime_hashes(source),
        "namespace": {"devbox_id": identity, "instance_id": active["instance_id"]},
        "authorization": {"quote": quote, "turn": "2026-10-07 21:18 EDT"},
    }
    q.bind_guest_runtime(binding, binaries)
    q.new_json(stage / "input.json", binding)
    q.new_json(local / "staged.json", binding)
    for filename in ("source.bundle", "source.patch", "input.json"):
        command(
            "upload", name, str(stage / filename), str(remote / filename), timeout=300
        )
    for filename in ("sonar_pr_issues.py", "gh_owner.py"):
        helper = Path.home() / "dev/og-workflow/.cursor/scripts" / filename
        binding[filename + "_sha256"] = q.sha256(helper)
        command("upload", name, str(helper), str(remote / filename))
    command("upload", name, str(secret), str(remote / "scanner-credential"))
    secret.unlink()
    setup = (
        "import hashlib,json,pathlib,subprocess,s"
        "ys; r=pathlib.Path(sys.argv[1]); b=json."
        "loads((r/'input.json').read_text()); ass"
        "ert hashlib.sha256((r/'source.bundle').r"
        "ead_bytes()).hexdigest()==b['bundle_sha2"
        "56']; assert hashlib.sha256((r/'source.p"
        "atch').read_bytes()).hexdigest()==b['sou"
        "rce_patch_sha256']; assert hashlib.sha25"
        "6((r/'guest-push-checks.py').read_bytes("
        ")).hexdigest()==b['guest_program_sha256'"
        "]; subprocess.run(['git','clone','--no-c"
        "heckout',str(r/'source.bundle'),str(r/'s"
        "ource')],check=True); subprocess.run(['g"
        "it','checkout','--detach',b['source_comm"
        "it']],cwd=r/'source',check=True); subpro"
        "cess.run(['git','apply',str(r/'source.pa"
        "tch')],cwd=r/'source',check=True) if (r/"
        "'source.patch').stat().st_size else None"
        "; assert all(hashlib.sha256((r/'source'/"
        "p).read_bytes()).hexdigest()==h for p,h "
        "in b['source_files'].items()); (r/'scann"
        "er-credential').chmod(0o600)"
    )
    command("exec", name, "--", "python3", "-c", setup, str(remote), timeout=300)
    detached = command(
        "exec",
        "--detach",
        name,
        "--",
        "python3",
        str(remote / "guest-push-checks.py"),
        str(remote),
    )
    execution = __import__("re").search(r"\bexec_[a-z0-9]+\b", detached)
    q.require(execution, "Namespace execution ID missing")
    q.new_json(
        local / "execution.json",
        {
            "execution_id": execution[0],
            "namespace": binding["namespace"],
            "remote": str(remote),
        },
    )
    print(
        json.dumps(
            {
                "phase": "guest-scans-started",
                "evidence": str(local),
                "execution_id": execution[0],
            }
        ),
        flush=True,
    )
    deadline = time.monotonic() + 2400
    while time.monotonic() < deadline:
        live = observe(identity)
        q.require(
            live["state"] == "running" and live["instance_id"] == active["instance_id"],
            "Guest instance changed",
        )
        status = subprocess.run(
            [
                cli,
                "exec",
                name,
                "--",
                "/bin/test",
                "-s",
                str(remote / "evidence/export.json"),
            ],
            env=env,
            capture_output=True,
            timeout=45,
        )
        q.require(status.returncode in {0, 1}, "Evidence observation failed")
        if status.returncode == 0:
            break
        time.sleep(10)
    else:
        raise TimeoutError("Guest scan evidence deadline")
    command(
        "download",
        name,
        str(remote / "evidence/export.json"),
        str(local / "export.json"),
    )
    exports = json.loads((local / "export.json").read_text())
    for filename, digest in exports["files"].items():
        q.evidence_name(filename)
        command(
            "download", name, str(remote / "evidence" / filename), str(local / filename)
        )
        q.require(
            q.sha256(q.regular(local / filename)) == digest, "Export hash differs"
        )
    result = json.loads((local / "push-checks.json").read_text())
    return {
        "guest_result": "pass" if result["success"] else "fail",
        "source_commit": commit,
        "source_patch_sha256": binding["source_patch_sha256"],
        "namespace": binding["namespace"],
        "execution_id": execution[0],
    }


record = None
try:
    token = os.environ.get("SONARQUBE_CLI_TOKEN") or os.environ.get("SONAR_TOKEN")
    q.require(token and "\n" not in token, "Configured scanner credential missing")
    with secret.open("x") as stream:
        secret.chmod(0o600)
        stream.write(token)
    del token
    targets = {
        Path(tempfile.gettempdir()) / ("namespace-owner-" + identity + ".lock"),
        Path("/private/tmp") / ("namespace-owner-" + identity + ".lock"),
    }
    for path in sorted(targets):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, json.dumps({
                "owner": "Task5 scoped landing lane d6ba92d8",
                "run_id": run_id, "pid": os.getpid(),
                "scope": "guest scans only; no issuer restoration",
                "authorization_quote": quote,
            }).encode())
            claims.append((fd, path))
        except BaseException:
            os.close(fd)
            raise
    with q.interruption_boundary():
        record = q.qualification_lifecycle(
            name,
            identity,
            work,
            observe,
            lambda: None,
            stop=lambda exact: command("stop", exact, "--force"),
        )
    record.update(
        authorization={"quote": quote, "turn": "2026-10-07 21:18 EDT"},
        observations=observations,
    )
    q.new_json(local / "host.json", record)
    print(json.dumps({"evidence": str(local), "record": record}), flush=True)
    if record.get("guest_result") != "pass" or not record.get("cleanup", {}).get(
        "verified"
    ):
        raise SystemExit(1)
finally:
    secret.unlink(missing_ok=True)
    for fd, path in claims:
        if record and record.get("cleanup", {}).get("verified"):
            held, live = os.fstat(fd), path.lstat()
            if (held.st_dev, held.st_ino) == (live.st_dev, live.st_ino):
                path.unlink()
        os.close(fd)
