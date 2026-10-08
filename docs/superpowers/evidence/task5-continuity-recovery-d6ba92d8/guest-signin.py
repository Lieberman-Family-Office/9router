#!/usr/bin/env python3
"Guest-only baseline preparation. Do not perform provider probes before confirmation."

import importlib.util
import json
import os
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
try:
    q.require(
        not q.BASELINE.exists(),
        "Existing guest baseline must be preserved; do not replace it",
    )
    locks, python, log = q.provision_dependencies(binding, run)
    (evidence / "dependencies.log").write_text(log)
    scope_file, scope, manifest = q.prepare_scope(Path(binding["tgz"]), binding)
    q.initialize_baseline(scope, manifest)
    os.environ["HOME"] = scope["home"]
    controller = q.controller_module()
    controller.configure_qualification(scope_file)
    package = scope["packages"][binding["sha256"]]
    # Prepare the existing enrollment transaction only through pinned worker readiness.
    # The authenticated readiness gate is untouched and runs after manual confirmation.
    with controller.deployment_lock():
        q.require(
            not controller.STATE_FILE.exists(),
            "Existing enrollment transaction requires explicit recovery",
        )
        controller.verify_enrollment_slots()
        controller.shared_environment(enrollment=True)
        entry = {
            "release": package["release"],
            "version": json.loads(
                (Path(package["release"]) / "package.json").read_text()
            )["version"],
            "digest": binding["sha256"],
            "mode": "starting",
        }
        state = {
            "schema": 1,
            "active": "a",
            "slots": {"a": entry, "b": None},
            "pending": None,
            "enrollment": "preparing",
            "environment_sha256": controller.sha256(
                controller.STATE_DIR / "environment.json"
            ),
        }
        controller.write_state(state)
        controller.prepare_enrollment(state, Path(entry["release"]), entry)
        controller.stop_enrollment_legacy_service(state)
        controller.start_worker("a", entry)
        status = controller.worker_status("a", controller.read_state()["slots"]["a"])
        q.require(
            status["mode"] == "ready" and status["appWork"]["initialized"],
            "Pinned sign-in worker not ready",
        )
    q.new_json(
        evidence / "checkpoint.json",
        {
            "phase": "manual-signin-required",
            "run_id": binding["run_id"],
            "namespace": binding["namespace"],
            "source_commit": binding["source_commit"],
            "sha256": binding["sha256"],
            "runtime_sha256": binding["runtime_sha256"],
            "scope": str(scope_file),
            "home": scope["home"],
            "dashboard_remote_port": 21128,
            "worker_pid": status["appPid"],
            "worker_label": controller.job_label("a"),
            "provider_probes_run": False,
            "baseline_receipt_written": False,
            "locked_dependencies": locks,
            "authorization": binding["authorization"],
        },
    )
except BaseException as error:
    q.new_json(
        evidence / "failure.json",
        {
            "type": type(error).__name__,
            "reason": str(error)
            if isinstance(error, ValueError)
            else "Guest setup failed",
        },
    )
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise
finally:
    q.new_json(
        evidence / "export.json",
        {"files": {p.name: q.sha256(p) for p in evidence.iterdir() if p.is_file()}},
    )
