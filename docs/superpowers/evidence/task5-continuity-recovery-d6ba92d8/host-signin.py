#!/usr/bin/env python3
"""Bounded host orchestration for approved independent guest sign-in."""

import fcntl
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import time
import types
import uuid
from pathlib import Path

source = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "qualifier", source / "scripts/mac/9router_vm_qualify.py"
)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
identity, name = "2tc0b2eg4mveo", "9router-qualify-recovery"
sdk = Path("/tmp").resolve() / "namespace-runtime-check-ef67fdaa"
cli = str(Path.home() / ".local/bin/devbox")
quote = (
    "Yes—provision the guest baseline; I will"
    " complete independent provider sign-in w"
    "hen prompted (Recommended)"
)
run_id = "task5-signin-" + uuid.uuid4().hex[:16]
remote = q.GUEST_ROOT / run_id
local = Path(__file__).parent / run_id
local.mkdir(mode=0o700)
stage = Path(tempfile.mkdtemp(prefix="task5-signin-stage-"))
claims, forwards = [], []
checkpoint = None
cli_env = {
    k: v
    for k, v in os.environ.items()
    if k not in {"SONAR_TOKEN", "SONARQUBE_CLI_TOKEN", "NINEROUTER_PROBE_KEY"}
}
tarball = (
    Path(__file__).parent / "task5-repro-aa6197854dc545c4/9router-0.5.91-lfenergy.7.tgz"
)


def owned():
    for fd, path in claims:
        live, held = path.lstat(), os.fstat(fd)
        q.require(
            (live.st_dev, live.st_ino) == (held.st_dev, held.st_ino),
            "Foreign lifecycle claim replaced our claim",
        )


def command(*args, timeout=180):
    owned()
    return q.output([cli, *args], source, cli_env, timeout)


def metadata(_identity):
    return q.host_metadata(identity, sdk)


def close():
    for process, log in forwards:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()


def wait_baseline_export(active):
    until = time.monotonic() + 1500
    while time.monotonic() < until:
        observed = metadata(identity)
        q.require(
            observed["state"] == "running"
            and observed["instance_id"] == active["instance_id"],
            "Guest identity changed",
        )
        seen = subprocess.run(
            [
                cli,
                ("exec"),
                name,
                ("--"),
                ("/bin/test"),
                ("-s"),
                str(remote / ("evidence/export.json")),
            ],
            env=cli_env,
            capture_output=True,
            timeout=45,
        )
        q.require(seen.returncode in {0, 1}, "Guest export observation failed")
        if seen.returncode == 0:
            return
        time.sleep(10)
    raise TimeoutError("Guest baseline setup deadline")


def work():
    command("exec", name, "--", "/usr/bin/uname", "-m")
    active = metadata(identity)
    q.require(
        active["state"] == "running"
        and active["id"] == identity
        and active["name"] == name
        and active["instance_id"],
        "Activated identity mismatch",
    )
    binding = q.host_stage(tarball, types.SimpleNamespace(source_file=[]), stage)
    binding.update(
        run_id=run_id,
        namespace={"devbox_id": identity, "instance_id": active["instance_id"]},
        tgz=str(remote / "candidate.tgz"),
        authorization={
            "quote": quote,
            "turn": "2026-10-07 16:33 EDT chat_selection task5_guest_authentication",
            ("activation_quote"): (
                "Namespace activation needs your explicit"
                " approval - EXPLICIT APPROVAL GRANTED"
            ),
            "activation_turn": "2026-10-07 15:09 EDT",
        },
    )
    command("exec", name, "--", "/bin/mkdir", "-p", "-m", "700", str(remote))
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
    q.bind_guest_runtime(binding, binaries)
    q.new_json(stage / "input.json", binding)
    q.new_json(local / "input.json", binding)
    guest = Path(__file__).parent / "guest-signin.py"
    for file in ["source.bundle", "qualification.patch", "candidate.tgz", "input.json"]:
        command("upload", name, str(stage / file), str(remote / file), timeout=300)
    command("upload", name, str(guest), str(remote / "guest-signin.py"))
    setup = (
        "import hashlib,json,pathlib,subprocess,s"
        "ys; r=pathlib.Path(sys.argv[1]); b=json."
        "loads((r/'input.json').read_text()); ass"
        "ert hashlib.sha256((r/'source.bundle').r"
        "ead_bytes()).hexdigest()==b['bundle_sha2"
        "56']; subprocess.run(['git','clone','--n"
        "o-checkout',str(r/'source.bundle'),str(r"
        "/'source')],check=True); subprocess.run("
        "['git','checkout','--detach',b['source_c"
        "ommit']],cwd=r/'source',check=True); p=r"
        "/'qualification.patch'; assert hashlib.s"
        "ha256(p.read_bytes()).hexdigest()==b['so"
        "urce_patch_sha256']; p.stat().st_size an"
        "d subprocess.run(['git','apply','--binar"
        "y',str(p)],cwd=r/'source',check=True); ("
        "r/'source/qualification.patch').write_by"
        "tes(p.read_bytes())"
    )
    command("exec", name, "--", "python3", "-c", setup, str(remote), timeout=300)
    execution = command(
        "exec",
        "--detach",
        name,
        "--",
        "python3",
        str(remote / "guest-signin.py"),
        str(remote),
    )
    match = re.search(r"\bexec_[a-z0-9]+\b", execution)
    q.require(match, "Guest setup execution ID missing")
    q.new_json(
        local / "execution.json",
        {
            "execution_id": match[0],
            "namespace": binding["namespace"],
            "remote": str(remote),
        },
    )
    wait_baseline_export(active)
    command(
        "download",
        name,
        str(remote / "evidence/export.json"),
        str(local / "export.json"),
    )
    exported = json.loads((local / "export.json").read_text())
    for filename, digest in exported["files"].items():
        q.evidence_name(filename)
        command(
            "download", name, str(remote / "evidence" / filename), str(local / filename)
        )
        q.require(
            q.sha256(q.regular(local / filename)) == digest,
            "Baseline evidence export differs",
        )
    q.require(
        (local / "checkpoint.json").exists(),
        "Guest setup refused; inspect exported failure.json",
    )
    checkpoint = json.loads((local / "checkpoint.json").read_text())
    # Do not replace occupied dashboard/callback ports or any existing forward client.
    for port in [32128, 1455]:
        observation = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
        )
        q.require(
            observation.returncode == 1 and not observation.stdout.strip(),
            "Private sign-in port already occupied: " + str(port),
        )
    log = (local / "port-forward.log").open("x")
    forward = subprocess.Popen(
        [cli, "port-forward", name, "--ports", "32128:21128,1455:1455"],
        env=cli_env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    forwards.append((forward, log))
    until = time.monotonic() + 60
    while time.monotonic() < until:
        q.require(forward.poll() is None, "Private port forward exited")
        observation = subprocess.run(
            [
                "/usr/sbin/lsof",
                "-nP",
                "-a",
                "-p",
                str(forward.pid),
                "-iTCP:32128",
                "-sTCP:LISTEN",
                "-Fpn",
            ],
            capture_output=True,
            text=True,
        )
        lines = [
            line[1:] for line in observation.stdout.splitlines() if line.startswith("n")
        ]
        if lines:
            q.require(
                all(
                    line in {"127.0.0.1:32128", "[::1]:32128", "localhost:32128"}
                    for line in lines
                ),
                "Dashboard forward is not loopback-only",
            )
            break
        time.sleep(1)
    else:
        raise TimeoutError("Private forward listener deadline")
    ready = {
        **checkpoint,
        "url": "http://localhost:32128/login",
        "provider_pages": [
            "http://localhost:32128/dashboard/providers/codex",
            "http://localhost:32128/dashboard/providers/claude",
        ],
        "forward_pid": forward.pid,
        "supervisor_pid": os.getpid(),
        "stop_file": str(local / "stop.request"),
        "completion_file": str(local / "signin-confirmed.json"),
        "sign_in_deadline_epoch": time.time() + 1200,
        ("dashboard_password_delivery"): (
            "Run the secret-to-clipboard command loca"
            "lly; no password enters chat or evidence"
        ),
    }
    q.new_json(local / "signin-ready.json", ready)
    print(
        json.dumps({"phase": "manual-signin-ready", "evidence": str(local), **ready}),
        flush=True,
    )
    until = time.monotonic() + 1200
    while time.monotonic() < until:
        owned()
        q.require(forward.poll() is None, "Owned dashboard forward exited")
        if (local / "stop.request").exists():
            return {
                "guest_result": "unrun",
                "checkpoint": "manual sign-in stopped",
                "evidence": str(local),
            }
        if (local / "signin-confirmed.json").exists():
            confirmation = q.private_json(local / "signin-confirmed.json")
            q.require(
                confirmation.get("operator_quote")
                and confirmation.get("operator_turn"),
                "Actual sign-in confirmation quote required",
            )
            return {
                "guest_result": "unrun",
                ("checkpoint"): (
                    "manual confirmation received; baseline completion still required"
                ),
                "evidence": str(local),
            }
        time.sleep(2)
    raise TimeoutError("Manual sign-in deadline; credentials retained, compute stopped")


try:
    for target in sorted(
        {
            Path(tempfile.gettempdir()) / ("namespace-owner-" + identity + ".lock"),
            Path("/private/tmp") / ("namespace-owner-" + identity + ".lock"),
        }
    ):
        fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(
                fd,
                json.dumps(
                    {
                        "owner": "1c7bce1a-1309-45c3-a156-cf5623c06fda",
                        "pid": os.getpid(),
                        "run_id": run_id,
                        "authorization_quote": quote,
                    }
                ).encode(),
            )
            claims.append((fd, target))
        except BaseException:
            os.close(fd)
            raise
    with q.interruption_boundary():
        result = q.qualification_lifecycle(
            name,
            identity,
            work,
            metadata,
            close,
            stop=lambda exact: command("stop", exact, "--force"),
        )
    q.new_json(local / "host-signin.json", result)
    print(
        json.dumps(
            {
                "phase": "sign-in-session-closed",
                "evidence": str(local),
                "result": result,
            }
        ),
        flush=True,
    )
    if result.get("cleanup", {}).get("verified"):
        owned()
        for fd, target in claims:
            target.unlink()
finally:
    for fd, target in claims:
        os.close(fd)
