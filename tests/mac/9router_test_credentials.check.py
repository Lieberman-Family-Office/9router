#!/usr/bin/env python3
"""Namespace-only fake restore/export dispatch proof; no actual credential paths."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

assert sys.platform == "darwin"
assert (
    subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.hv_vmm_present"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    == "1"
)
script = Path(__file__).resolve().parents[2] / "scripts/mac/9router_test_credentials.py"
spec = importlib.util.spec_from_file_location("checkpoint", script)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
os.umask(0o077)


def fixture(root, generation, state="done"):
    stage = root / (".stage-" + str(generation))
    stage.mkdir(mode=0o700)
    app = sqlite3.connect(stage / "app.sqlite")
    app.executescript(
        (
            "CREATE TABLE providerConnections(id TEXT"
            ",provider TEXT,authType TEXT,isActive IN"
            "TEGER,data TEXT); CREATE TABLE apiKeys(i"
            "d TEXT,key TEXT,isActive INTEGER); CREAT"
            "E TABLE settings(id INTEGER,data TEXT);"
        )
    )
    for provider in ("claude", "codex"):
        app.execute(
            "INSERT INTO providerConnections VALUES(?,?,?,?,?)",
            (
                provider + "-test",
                provider,
                "oauth",
                1,
                json.dumps(
                    {
                        "refreshToken": "fictional-rotated-" + str(generation),
                        "refreshGenerations": {"oauth": generation},
                    }
                ),
            ),
        )
    app.execute(
        "INSERT INTO apiKeys VALUES('fictional-id',?,1)", ("fictional-guest-key",)
    )
    app.execute(
        "INSERT INTO settings VALUES(1,?)",
        ('{"tunnelEnabled":false,"tailscaleEnabled":false,"mitmEnabled":false}',),
    )
    app.commit()
    app.close()
    refresh = sqlite3.connect(stage / "refresh.sqlite")
    refresh.executescript(
        (
            "CREATE TABLE refresh_meta(id INTEGER,pro"
            "tocol INTEGER); INSERT INTO refresh_meta"
            " VALUES(1,1); CREATE TABLE refresh_seque"
            "nce(id INTEGER,value INTEGER); CREATE TA"
            "BLE refresh_flights(key TEXT,owner TEXT,"
            "state TEXT,result TEXT,generation INTEGE"
            "R,started_at TEXT);"
        )
    )
    refresh.execute("INSERT INTO refresh_sequence VALUES(1,?)", (generation,))
    refresh.execute(
        "INSERT INTO refresh_flights VALUES(?,?,?,?,?,?)",
        (
            "fictional-digest",
            "fictional-owner",
            state,
            json.dumps(
                {
                    "accessToken": "fictional-access",
                    "refreshToken": "fictional-rotated-" + str(generation),
                    "refreshGenerations": {"oauth": generation},
                }
            ),
            generation,
            "fictional-time",
        ),
    )
    refresh.commit()
    refresh.close()
    (stage / "env.sh").write_text(
        (
            'export DATA_DIR="/fictional/test/.9route'
            'r"\nexport JWT_SECRET="fictional-jwt"\nexp'
            'ort API_KEY_SECRET="fictional-api"\nexpor'
            't MACHINE_ID_SALT="fictional-salt"\nexpor'
            't INITIAL_PASSWORD="fictional-password"\n'
            'export NODE_ENV="production"\nexport REQU'
            'EST_DETAILS_MODE="disabled"\n'
        )
    )
    (stage / "schema-manifest.json").write_text(
        '{"protocol":1,"persistenceFingerprint":"fictional-schema"}'
    )
    for name in c.FILES:
        (stage / name).chmod(0o600)
    c.save(
        stage / "manifest.json",
        {
            "schema": 1,
            "kind": "private-test-credential-seed-not-qualification",
            "credential_origin": "fresh-independently-authenticated-guest-only",
            "provider_counts": {"claude": 1, "codex": 1},
            "active_api_key_count": 1,
            "refresh_sequence": generation,
            "run_id": "fictional",
            "files": {name: c.digest(stage / name) for name in c.FILES},
        },
    )
    return stage


def refused(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("Expected refusal")


with tempfile.TemporaryDirectory(
    prefix="credential-check-", dir=str(Path.home())
) as temporary:
    root = Path(temporary).resolve()
    with c.owner(root):
        refused(lambda: c.owner(root).__enter__())
        seed = fixture(root, 1)
        destination = root / ("checkpoint-" + "a" * 32)
        os.rename(seed, destination)
        c.save(
            root / "latest.json",
            {
                "checkpoint": destination.name,
                "manifest_sha256": c.digest(destination / "manifest.json"),
            },
        )
        previous = c.digest(root / "latest.json")
        c.begin(
            root,
            {
                "run_id": "fictional-run",
                "namespace": {"devbox_id": "fake", "instance_id": "fake"},
            },
        )
        refused(lambda: c.current(root))
        stage = fixture(root, 2)
        with patch.object(
            c.os, "replace", side_effect=OSError("fictional publish failure")
        ):
            try:
                c.publish(root, stage, previous)
            except OSError:
                pass
            else:
                raise AssertionError("Failed export passed")
        assert (
            c.digest(root / "latest.json") == previous
            and (root / "uncertain.json").exists()
        )
        stage = fixture(root, 3)
        summary = c.publish(root, stage, previous)
        assert (
            summary["refresh_sequence"] == 3 and not (root / "uncertain.json").exists()
        )
        seed = c.current(root, "fictional-schema")
        home = root / "home"
        home.mkdir(mode=0o700)
        for name in (".9router", ".9router/db", ".9router/hotswap"):
            (home / name).mkdir(mode=0o700)
        c.restore(seed, home, "fictional-schema")
        app = sqlite3.connect(home / ".9router/db/data.sqlite")
        assert all(
            json.loads(row[0])["refreshToken"] == "fictional-rotated-3"
            for row in app.execute("SELECT data FROM providerConnections")
        )
        app.close()
        refresh = sqlite3.connect(home / ".9router/hotswap/refresh.sqlite")
        assert refresh.execute("SELECT value FROM refresh_sequence").fetchone()[0] == 3
        assert (
            json.loads(
                refresh.execute("SELECT result FROM refresh_flights").fetchone()[0]
            )["refreshToken"]
            == "fictional-rotated-3"
        )
        refresh.close()
        assert (
            'export DATA_DIR="' + str(home / ".9router") + '"'
            in (home / ".9router/env.sh").read_text()
        )
        refused(lambda: c.restore(seed, home, "fictional-schema"))
        refused(lambda: c.current(root, "wrong-schema"))
        # Enter the actual final export dispatch using fictional private worker state.
        c.save(
            home / ".9router/hotswap/manifest.json",
            json.loads((seed / "schema-manifest.json").read_text()),
        )

        class Controller:
            def configure_qualification(self, scope):
                assert scope == root / "scope.json"

            def control(self, slot, op):
                assert (slot, op) == ("a", "drain")

            def read_state(self):
                return {"slots": {"a": {"release": "fictional"}}}

            def verify_job(self, slot, entry):
                assert slot == "a"

            def worker_status(self, slot, entry):
                return {
                    "connections": 0,
                    "appWork": {
                        "initialized": True,
                        "unknown": False,
                        "draining": True,
                        **dict.fromkeys(c.WORK, 0),
                    },
                }

        exported = root / "exported"
        c.export(
            home,
            root / "scope.json",
            Controller(),
            exported,
            {
                "run_id": "fictional-final",
                "namespace": {"devbox_id": "fake", "instance_id": "fake"},
                "sha256": "a" * 64,
                "source_commit": "b" * 40,
            },
        )
        checked, account_generations = c.validate(exported, "fictional-schema")
        assert checked["refresh_sequence"] == 3 and all(
            value["oauth"] == 3 for value in account_generations.values()
        )
        c.begin(root, {"run_id": "fictional-next", "namespace": {}})
        stale = fixture(root, 0)
        refused(lambda: c.publish(root, stale, c.digest(root / "latest.json")))
        assert (root / "uncertain.json").exists()
        uncertain = fixture(root, 4, "uncertain")
        refused(lambda: c.publish(root, uncertain, c.digest(root / "latest.json")))
    refused(lambda: c.owner(root).__enter__())
def preuse_fixture(root):
    seed = fixture(root, 1)
    destination = root / ("checkpoint-" + "b" * 32)
    os.rename(seed, destination)
    c.save(root / "latest.json", {"checkpoint": destination.name,
           "manifest_sha256": c.digest(destination / "manifest.json")})
    binding = {"run_id": "qualify-fictional-preuse", "source_commit": "a" * 40,
               "source_patch_sha256": "b" * 64,
               "namespace": {"devbox_id": "fake", "instance_id": "fake-instance"}}
    c.begin(root, binding)
    marker = c.digest(root / "uncertain.json")
    latest = c.digest(root / "latest.json")
    files = json.loads((destination / "manifest.json").read_text())["files"]
    evidence = {
        "binding": binding,
        "failure": {"type": "GuestCheckFailure", "frames": ["cmd_guest",
                    "prerequisite_checks", "prerequisite_native_checks", "guest_command"]},
        "activation_order": {"prerequisite_checks": 10, "live_scope_for_run": 20,
                             "live_authentication": 30},
        "source_functions": {"cmd_guest": {"sha256": "d" * 64}},
        "source_files": {"scripts/mac/9router_vm_qualify.py": {
            "sha256": "c" * 64, "matches_input_binding": True}},
        "artifact_observations": {name: {"exists": False, "observation": "os.lstat ENOENT"}
                                  for name in ("credential-scope.json", "live-home")},
        "seed_files": {name: {"guest_sha256": digest,
                              "host_retained_manifest_sha256": digest, "equal": True}
                       for name, digest in files.items()},
        "fixture_facts": {"provider_checkpoint_path_in_native_fixture": False,
                          "host_source_patch_bytes": 0},
    }
    proof = root / "preuse-evidence.json"
    c.save(proof, evidence)
    inspection = {
        "inspection_completed": True, "uncertainty_unchanged": True,
        "evidence_sha256": c.digest(proof), "uncertainty_sha256_before": marker,
        "input": {**binding, "original_namespace": binding["namespace"],
                  "latest_pointer_sha256": latest, "seed_files": files,
                  "seed_manifest_sha256": c.digest(destination / "manifest.json")},
        "cleanup": {"verified": True, "stop_succeeded": True, "observations": [
            {"id": "fake", "name": "fake-guest", "state": "stopped",
             "instanceId": None, "at": "2026-10-08T00:00:00Z"},
            {"id": "fake", "name": "fake-guest", "state": "stopped",
             "instanceId": None, "at": "2026-10-08T00:01:01Z"}]},
    }
    report = root / "inspection.json"
    c.save(report, inspection)
    binding.update(evidence_sha256=c.digest(proof), inspection_sha256=c.digest(report),
                   preuse_control_sha256="d" * 64)
    return binding, proof, report, marker, latest, files


for mutation in (None, "missing", "wrong-run", "changed-seed", "post-use", "forged",
                 "shutdown", "receipt-failure", "disposition-failure"):
    with tempfile.TemporaryDirectory(prefix="preuse-check-", dir=str(Path.home())) as tmp:
        root = Path(tmp).resolve()
        binding, proof, report, marker, latest, files = preuse_fixture(root)
        if mutation in {"wrong-run", "post-use"}:
            value = json.loads(proof.read_text())
            if mutation == "wrong-run": value["binding"]["run_id"] = "different-run"
            else: value["artifact_observations"]["credential-scope.json"]["exists"] = True
            proof.write_text(json.dumps(value))
            binding["evidence_sha256"] = c.digest(proof)
            inspection = json.loads(report.read_text())
            inspection["evidence_sha256"] = binding["evidence_sha256"]
            report.write_text(json.dumps(inspection))
            binding["inspection_sha256"] = c.digest(report)
        elif mutation == "changed-seed":
            destination = root / json.loads((root / "latest.json").read_text())["checkpoint"]
            (destination / "env.sh").write_text("changed seed")
        elif mutation == "missing": proof.unlink()
        elif mutation == "forged": proof.write_text(proof.read_text() + " ")
        elif mutation == "shutdown":
            inspection = json.loads(report.read_text())
            inspection["cleanup"]["observations"][1]["state"] = "running"
            report.write_text(json.dumps(inspection))
            binding["inspection_sha256"] = c.digest(report)
        try:
            with patch.object(c.os, "rename", side_effect=OSError("fictional disposition failure")) if mutation == "disposition-failure" else patch.object(c, "sync", side_effect=OSError("fictional receipt failure")) if mutation == "receipt-failure" else __import__("contextlib").nullcontext():
                c.abort_precredential_run(root, proof, report, binding)
        except (OSError, ValueError):
            assert mutation is not None
            assert c.digest(root / "uncertain.json") == marker
            assert c.digest(root / "latest.json") == latest
        else:
            assert mutation is None
            assert not (root / "uncertain.json").exists()
            archived = root / ("preuse-aborted-" + binding["run_id"] + ".json")
            assert c.digest(archived) == marker
            assert c.digest(root / "latest.json") == latest
            with c.owner(root):
                seed = c.current(root)
                assert all(c.digest(seed / name) == digest for name, digest in files.items())
        if mutation == "forged":
            refused(lambda: c.owner(root).__enter__())
print("PASS: pre-use abort genuine recovery and eight mutated/failure cases; seed/key unchanged")
print(
    (
        "PASS: private fake latest-token/generati"
        "on restore; second owner, schema/stale/u"
        "ncertain state and failed export refuse;"
        " previous checkpoint preserved"
    )
)
