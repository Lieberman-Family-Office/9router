#!/usr/bin/env python3
"Create exactly one approved stopped guest. Preserve any returned storage on failure."

import fcntl
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "qualifier", root / "scripts/mac/9router_vm_qualify.py"
)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
folder = Path(__file__).parent
sdk = Path("/tmp").resolve() / "namespace-runtime-check-ef67fdaa"
name = "9router-task5-fresh300-d6ba92d8-20261007"
api = folder / "fresh-guest-api.mjs"
receipt = folder / "fresh-creation.json"
claim_path = Path(tempfile.gettempdir()) / ("namespace-create-owner-" + name + ".lock")
if claim_path.exists():
    previous = json.loads(claim_path.read_text())
    q.require(
        previous.get("owner") == "1c7bce1a-1309-45c3-a156-cf5623c06fda"
        and previous.get("name") == name,
        "Foreign creation claim",
    )
    try:
        os.kill(previous["pid"], 0)
    except ProcessLookupError:
        pass
    else:
        raise ValueError("Previous creation owner still running")
    fd = os.open(claim_path, os.O_RDWR)
else:
    fd = os.open(claim_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.write(
    fd,
    json.dumps(
        {
            "owner": "1c7bce1a-1309-45c3-a156-cf5623c06fda",
            "pid": os.getpid(),
            "name": name,
            "scope": "exactly one approved stopped creation",
        }
    ).encode(),
)
record = {
    "authorization": {
        ("quote"): (
            "Create one new isolated 300 GB macOS gue"
            "st, preserve the old guest, and prove SQ"
            "Lite persistence before another independ"
            "ent sign-in (Recommended)"
        ),
        "turn": "2026-10-07 18:11 EDT guest_persistence_path answer",
    },
    "name": name,
    "created": False,
}
box = None
try:
    q.require(
        not receipt.exists(),
        "Fresh creation already attempted; inspect receipt, never create another guest",
    )
    result = subprocess.run(
        ["node", str(api), str(sdk), "create", name, str(receipt)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    record["creation_exit_code"] = result.returncode
    record["creation_diagnostic"] = result.stderr[-6000:]
    q.require(
        result.returncode == 0 and receipt.exists(),
        "Creation outcome unknown; resolve exact unique name before retry",
    )
    created = q.private_json(receipt)
    box = json.loads(
        q.output(
            ["node", str(api), str(sdk), "metadata", created["returned"]["id"]],
            root,
            timeout=60,
        )
    )
    q.require(
        box["name"] == name
        and box["volumeSizeGb"] == "300"
        and box["os"] == "macos"
        and box["architecture"] == "arm64"
        and box["accessMode"] == 1
        and box["ephemeral"] is None
        and not box.get("repository")
        and not box.get("versionControl", {}).get("repositories")
        and not box.get("integrationsFields"),
        "Fresh identity or isolation differs",
    )
    record.update(created=True, verified=box)
finally:
    # Creation was explicitly stopped.
    # If the response is uncertain, inspect only the unique name.
    if box is None:
        observed = subprocess.run(
            ["node", str(api), str(sdk), "metadata", name],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if observed.returncode == 0:
            box = json.loads(observed.stdout)
            record["resolved_after_uncertainty"] = box
        else:
            record["name_resolution"] = (
                "unknown or not found; do not repeat creation blindly"
            )
    if box:
        if box["state"] != "stopped" or box["instance_id"] is not None:
            subprocess.run(
                [str(Path.home() / ".local/bin/devbox"), "stop", name, "--force"],
                check=True,
                capture_output=True,
                timeout=120,
            )
        observations = []
        for index in range(2):
            if index:
                __import__("time").sleep(60)
            observations.append(
                {"at": __import__("time").time(), **q.host_metadata(box["id"], sdk)}
            )
        record["stopped_observations"] = observations
        q.require(
            all(
                item["name"] == name
                and item["state"] == "stopped"
                and item["instance_id"] is None
                for item in observations
            ),
            "Fresh stopped creation verification incomplete",
        )
        record["verified_stopped"] = True
    q.new_json(folder / ("fresh-creation-host-" + str(os.getpid()) + ".json"), record)
    if record.get("verified_stopped"):
        claim_path.unlink()
    os.close(fd)
print(json.dumps(record))
