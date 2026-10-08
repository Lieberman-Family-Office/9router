#!/usr/bin/env python3
"Complete the retained guest enrollment after actual independent sign-in confirmation."

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

run = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "qualifier", run / "source/scripts/mac/9router_vm_qualify.py"
)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
q.guest_guard()
os.umask(0o077)
binding = q.private_json(run / "input.json")
q.source_binding(run / "source", binding)
q.runtime_preflight(binding, run)
checkpoint = q.private_json(run / "evidence/checkpoint.json")
confirmation = q.private_json(run / "confirmation.json")
q.require(
    confirmation["operator_quote"]
    == "both providers have been signed into and authenticated",
    "Actual sign-in confirmation missing",
)
home = Path(checkpoint["home"])
os.environ["HOME"] = str(home)
with sqlite3.connect(
    "file:" + str(home / ".9router/db/data.sqlite") + "?mode=ro", uri=True
) as db:
    counts = {
        row[0]: row[1]
        for row in db.execute(
            (
                "SELECT provider,count(*) FROM providerCo"
                "nnections WHERE isActive=1 AND authType="
                "'oauth' AND provider IN ('codex','claude"
                "') GROUP BY provider"
            )
        )
    }
    keys = db.execute("SELECT count(*) FROM apiKeys WHERE isActive=1").fetchone()[0]
q.require(
    all(counts.get(provider, 0) > 0 for provider in ("codex", "claude")) and keys > 0,
    "Guest provider or API key population absent",
)
controller = q.controller_module()
controller.configure_qualification(Path(checkpoint["scope"]))
with controller.deployment_lock():
    state = controller.reconcile_locked()
    q.require(
        state.get("enrollment") == "complete" and state.get("pending") is None,
        "Enrollment readiness did not complete",
    )
baseline = {
    "protocol": 1,
    "run_id": binding["run_id"],
    "devbox_id": binding["namespace"]["devbox_id"],
    "credential_origin": "guest-owned",
    "scope": checkpoint["scope"],
    "source_commit": binding["source_commit"],
    "sha256": binding["sha256"],
    "authentication_confirmation": confirmation,
    "active_provider_counts": counts,
    "active_api_key_count": keys,
}
q.require(not q.BASELINE.exists(), "Existing baseline marker must not be overwritten")
q.new_json(q.BASELINE, baseline)
q.new_json(
    run / "evidence/baseline-complete.json",
    {
        **baseline,
        "namespace": binding["namespace"],
        "runtime_sha256": binding["runtime_sha256"],
        "enrollment": "complete",
        ("readiness_probe"): (
            "mandatory controller private and public authenticated completion"
        ),
        "production_touched": False,
    },
)
print(
    json.dumps(
        {
            "phase": "baseline-complete",
            "active_provider_counts": counts,
            "active_api_key_count": keys,
            "run_id": binding["run_id"],
            "sha256": binding["sha256"],
        }
    )
)
