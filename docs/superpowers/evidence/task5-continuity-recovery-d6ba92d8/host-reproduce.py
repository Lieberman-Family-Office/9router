#!/usr/bin/env python3
"Host orchestration only. All dependency, build and fixture processes run in Namespace."

import fcntl
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
sdk = Path("/tmp").resolve() / "namespace-runtime-check-ef67fdaa"
identity = "2tc0b2eg4mveo"
name = "9router-qualify-recovery"
quote = "Namespace activation needs your explicit approval - EXPLICIT APPROVAL GRANTED"
owner = "1c7bce1a-1309-45c3-a156-cf5623c06fda"
run_id = "task5-repro-" + uuid.uuid4().hex[:16]
remote = q.GUEST_ROOT / run_id
local = Path(__file__).parent / run_id
local.mkdir(mode=0o700)
stage = Path(tempfile.mkdtemp(prefix="task5-repro-stage-"))
cli = str(Path.home() / ".local/bin/devbox")
cli_env = {
    k: v
    for k, v in os.environ.items()
    if k not in {"SONAR_TOKEN", "SONARQUBE_CLI_TOKEN", "NINEROUTER_PROBE_KEY"}
}
claims = []
observations = []


def observe(_identity):
    data = q.host_metadata(identity, sdk)
    observations.append({"at": time.time(), **data})
    return data


def command(*argv, timeout=180):
    for descriptor, target in claims:
        held, live = os.fstat(descriptor), target.lstat()
        q.require(
            (held.st_dev, held.st_ino) == (live.st_dev, live.st_ino),
            "lifecycle claim changed",
        )
    result = q.output([cli, *argv], source, cli_env, timeout)
    print(
        json.dumps(
            {
                "phase": "host-orchestration",
                "command": list(argv[:3]),
                "output": result[-1200:],
            }
        ),
        flush=True,
    )
    return result


def work():
    command("exec", name, "--", "/usr/bin/uname", "-m")
    active = observe(identity)
    q.require(
        active["id"] == identity
        and active["name"] == name
        and active["state"] == "running"
        and active["instance_id"],
        "activated identity mismatch",
    )
    command("exec", name, "--", "/bin/mkdir", "-p", "-m", "700", str(remote))
    program = Path(__file__).parent / "guest-reproduce.py"
    command("upload", name, str(program), str(remote / "guest-reproduce.py"))
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
    binding = {
        "protocol": 1,
        "run_id": run_id,
        "source_commit": q.output(["git", "rev-parse", "HEAD"], source),
        "source_patch_sha256": __import__("hashlib").sha256(b"").hexdigest(),
        "namespace": {"devbox_id": identity, "instance_id": active["instance_id"]},
        "authorization": {"quote": quote, "turn": "2026-10-07 15:09 EDT"},
        "source_runtime_sha256": q.source_runtime_hashes(source),
    }
    tracked = q.output(["git", "ls-files", "-z"], source).split("\0")
    binding["source_files"] = {
        p: q.sha256(source / p) for p in tracked if p and not p.startswith("docs/")
    }
    q.bind_guest_runtime(binding, binaries)
    bundle = stage / "source.bundle"
    refs = q.output(
        ["git", "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"],
        source,
    ).splitlines()
    q.output(
        ["git", "bundle", "create", str(bundle), "HEAD", *refs], source, timeout=180
    )
    binding["bundle_sha256"] = q.sha256(bundle)
    binding["guest_program_sha256"] = q.sha256(program)
    q.new_json(stage / "input.json", binding)
    q.new_json(local / "staged.json", binding)
    command("upload", name, str(bundle), str(remote / "source.bundle"))
    command("upload", name, str(stage / "input.json"), str(remote / "input.json"))
    setup = (
        "import hashlib,json,pathlib,subprocess,s"
        "ys; r=pathlib.Path(sys.argv[1]); b=json."
        "loads((r/'input.json').read_text()); ass"
        "ert hashlib.sha256((r/'source.bundle').r"
        "ead_bytes()).hexdigest()==b['bundle_sha2"
        "56']; assert hashlib.sha256((r/'guest-re"
        "produce.py').read_bytes()).hexdigest()=="
        "b['guest_program_sha256']; subprocess.ru"
        "n(['git','clone','--no-checkout',str(r/'"
        "source.bundle'),str(r/'source')],check=T"
        "rue); subprocess.run(['git','checkout','"
        "--detach',b['source_commit']],cwd=r/'sou"
        "rce',check=True); (r/'source/qualificati"
        "on.patch').write_bytes(b'')"
    )
    command("exec", name, "--", "python3", "-c", setup, str(remote), timeout=300)
    detached = command(
        "exec",
        "--detach",
        name,
        "--",
        "python3",
        str(remote / "guest-reproduce.py"),
        str(remote),
    )
    execution = __import__("re").search(r"\bexec_[a-z0-9]+\b", detached)
    q.require(execution, "Namespace execution ID missing")
    q.new_json(
        local / "execution.json",
        {
            "execution_id": execution[0],
            "remote": str(remote),
            "namespace": binding["namespace"],
        },
    )
    deadline = time.monotonic() + 7200
    while time.monotonic() < deadline:
        observed = observe(identity)
        q.require(
            observed["state"] == "running"
            and observed["instance_id"] == active["instance_id"],
            "guest instance changed",
        )
        test = subprocess.run(
            [
                cli,
                "exec",
                name,
                "--",
                "/bin/test",
                "-s",
                str(remote / "evidence/export.json"),
            ],
            env=cli_env,
            capture_output=True,
            timeout=45,
        )
        q.require(test.returncode in {0, 1}, "guest observation failed")
        if test.returncode == 0:
            break
        time.sleep(10)
    else:
        raise TimeoutError("guest reproduction evidence deadline")
    command(
        "download",
        name,
        str(remote / "evidence/export.json"),
        str(local / "export.json"),
    )
    exported = json.loads((local / "export.json").read_text())
    for filename, expected in exported["files"].items():
        q.evidence_name(filename)
        command(
            "download", name, str(remote / "evidence" / filename), str(local / filename)
        )
        q.require(
            q.sha256(q.regular(local / filename)) == expected, "export digest differs"
        )
    if exported.get("tarball"):
        filename = q.evidence_name(exported["tarball"])
        command(
            "download", name, str(remote / filename), str(local / filename), timeout=300
        )
        q.require(
            q.sha256(q.regular(local / filename)) == exported["sha256"],
            "produced tarball digest differs",
        )
    result = json.loads((local / "guest-reproduction.json").read_text())
    return {
        "guest_result": "pass" if result["success"] else "fail",
        "source_commit": binding["source_commit"],
        "namespace": binding["namespace"],
        "execution_id": execution[0],
        "remote": str(remote),
        "qualification_receipt": False,
    }


try:
    # Claim both known temporary-root spellings. Never unlink a foreign claim.
    targets = {
        Path(tempfile.gettempdir()) / ("namespace-owner-" + identity + ".lock"),
        Path("/private/tmp") / ("namespace-owner-" + identity + ".lock"),
    }
    for target in sorted(targets):
        descriptor = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(
                descriptor,
                json.dumps(
                    {
                        "owner": owner,
                        "run_id": run_id,
                        "scope": "Task5 reproduction",
                        "pid": os.getpid(),
                        "authorization_quote": quote,
                    }
                ).encode(),
            )
            claims.append((descriptor, target))
        except BaseException:
            os.close(descriptor)
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
    record["authorization"] = {"quote": quote, "turn": "2026-10-07 15:09 EDT"}
    record["owner"] = owner
    record["observations"] = observations
    q.new_json(local / "host-reproduction.json", record)
    print(json.dumps({"evidence": str(local), "record": record}), flush=True)
    if record.get("cleanup", {}).get("verified"):
        for descriptor, target in claims:
            held, live = os.fstat(descriptor), target.lstat()
            if (held.st_dev, held.st_ino) == (live.st_dev, live.st_ino):
                target.unlink()
finally:
    for descriptor, target in claims:
        os.close(descriptor)
