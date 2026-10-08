#!/usr/bin/env python3
"""Private independent test-account checkpoints; never deployment receipts.

ponytail: one owner and two SQLite backups. Add encrypted off-host recovery only
with separate approval. An uncertain issuer run refuses reuse, not lock takeover.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

FILES = ("app.sqlite", "refresh.sqlite", "env.sh", "schema-manifest.json")
WORK = (
    "responses",
    "handlers",
    "upgrades",
    "cleanup",
    "persistence",
    "refresh",
    "background",
    "quota",
    "websocket",
)


def require(value, reason):
    if not value:
        raise ValueError(reason)


def private(path, directory=False):
    path = Path(path)
    info = path.lstat()
    require(
        path.is_absolute()
        and path.resolve() == path
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
        and (
            stat.S_ISDIR(info.st_mode)
            if directory
            else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        ),
        "Private test checkpoint path refused",
    )
    for parent in path.parents:
        info = parent.lstat()
        require(
            not stat.S_ISLNK(info.st_mode)
            and stat.S_ISDIR(info.st_mode)
            and info.st_uid in {0, os.getuid()}
            and not info.st_mode & 0o022,
            "Unsafe checkpoint ancestor",
        )
    return path


def digest(path):
    with private(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def environment(path):
    allowed = {
        "DATA_DIR",
        "JWT_SECRET",
        "API_KEY_SECRET",
        "MACHINE_ID_SALT",
        "INITIAL_PASSWORD",
        "NODE_ENV",
        "REQUEST_DETAILS_MODE",
    }
    values = {}
    for line in private(path).read_text().splitlines():
        match = re.fullmatch(r'export ([A-Z_]+)="([^"$`\r\n]*)"', line)
        require(
            match and match[1] in allowed and match[1] not in values,
            "Test environment literals refused",
        )
        values[match[1]] = match[2]
    require(
        set(values) == allowed and all(values[name] for name in allowed),
        "Test environment incomplete",
    )
    require(
        values["NODE_ENV"] == "production"
        and values["REQUEST_DETAILS_MODE"] == "disabled",
        "Test environment mode refused",
    )
    return values


def validate_account_generations(rows, refresh, sequence):
    accounts = {}
    for account, provider, _auth, _active, raw in rows:
        data = json.loads(raw)
        generations = data.get("refreshGenerations", {})
        require(
            isinstance(data, dict)
            and "refreshGeneration" not in data
            and isinstance(generations, dict)
            and set(generations) <= {"oauth", "copilot"}
            and all(
                type(value) is int and 0 < value <= sequence
                for value in generations.values()
            ),
            "Account CAS generation differs",
        )
        accounts[(account, provider)] = generations
    for state, raw, generation in refresh.execute(
        "SELECT state,result,generation FROM refresh_flights"
    ):
        value = json.loads(raw)
        require(
            state == "done"
            and type(generation) is int
            and 0 < generation <= sequence
            and isinstance(value, dict)
            and len(value.get("refreshGenerations", {})) == 1
            and next(iter(value["refreshGenerations"].values())) == generation
            and any(
                isinstance(value.get(key), str) and value[key]
                for key in ("accessToken", "apiKey", "token", "copilotToken")
            ),
            "Durable issuer result refused",
        )
    return accounts


def validate_checkpoint_layout(app, schema):
    if not schema.get("layout"):
        return
    layout = [
        dict(zip(("type", "name", "tbl_name", "sql"), row))
        for row in app.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]
    for row in layout:
        row["sql"] = re.sub(r"\s+", " ", row["sql"]).strip() if row["sql"] else None
    require(layout == schema["layout"], "Checkpoint SQLite layout differs")


def validate(folder, fingerprint=None):
    """No secret value is returned or included in validation errors."""
    private(folder, True)
    manifest = json.loads(private(folder / "manifest.json").read_text())
    require(
        manifest.get("schema") == 1
        and manifest.get("kind") == "private-test-credential-seed-not-qualification"
        and manifest.get("credential_origin")
        == "fresh-independently-authenticated-guest-only"
        and set(manifest.get("files", {})) == set(FILES),
        "Checkpoint provenance refused",
    )
    for name in FILES:
        require(
            digest(folder / name) == manifest["files"][name],
            "Checkpoint digest differs",
        )
    schema = json.loads(private(folder / "schema-manifest.json").read_text())
    require(
        schema.get("protocol") == 1
        and schema.get("persistenceFingerprint")
        and (fingerprint is None or schema["persistenceFingerprint"] == fingerprint),
        "Checkpoint schema differs",
    )
    environment(folder / "env.sh")
    databases = []
    try:
        for name in FILES[:2]:
            databases.append(
                sqlite3.connect((folder / name).as_uri() + "?mode=ro", uri=True)
            )
        app, refresh = databases
        require(
            all(
                db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                for db in databases
            ),
            "Checkpoint SQLite integrity refused",
        )
        validate_checkpoint_layout(app, schema)
        rows = app.execute(
            (
                "SELECT id,provider,authType,isActive,data "
                "FROM providerConnections ORDER BY provider"
            )
        ).fetchall()
        require(
            len(rows) == 2
            and [(row[1], row[2], row[3]) for row in rows]
            == [("claude", "oauth", 1), ("codex", "oauth", 1)],
            "Checkpoint must contain two test accounts only",
        )
        sequence = refresh.execute(
            "SELECT value FROM refresh_sequence WHERE id=1"
        ).fetchone()[0]
        require(
            type(sequence) is int
            and sequence >= 0
            and sequence == manifest.get("refresh_sequence"),
            "Checkpoint generation refused",
        )
        require(
            refresh.execute("SELECT protocol FROM refresh_meta WHERE id=1").fetchone()[
                0
            ]
            == 1,
            "Refresh protocol refused",
        )
        states = dict(
            refresh.execute("SELECT state,count(*) FROM refresh_flights GROUP BY state")
        )
        require(
            set(states) <= {"done"}, "Uncertain or pending issuer state refuses reuse"
        )
        accounts = validate_account_generations(rows, refresh, sequence)
        keys = app.execute("SELECT count(*) FROM apiKeys WHERE isActive=1").fetchone()[
            0
        ]
        require(
            keys > 0 and manifest.get("active_api_key_count", keys) == keys,
            "Guest API key population differs",
        )
        settings = json.loads(
            app.execute("SELECT data FROM settings WHERE id=1").fetchone()[0]
        )
        require(
            not any(
                settings.get(name)
                for name in ("tunnelEnabled", "tailscaleEnabled", "mitmEnabled")
            ),
            "External guest integration refused",
        )
        require(
            manifest.get("provider_counts") == {"claude": 1, "codex": 1},
            "Declared account population differs",
        )
        return manifest, accounts
    except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError, IndexError):
        raise ValueError("Checkpoint contents refused") from None
    finally:
        for database in databases:
            database.close()


@contextmanager
def owner(root):
    root = Path(root).absolute()
    root.mkdir(mode=0o700, exist_ok=True)
    private(root, True)
    descriptor = os.open(
        root / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        private(root / "owner.lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another test credential owner is active") from None
        require(
            not (root / "uncertain.json").exists(),
            "Uncertain credential state refuses next issuer run",
        )
        yield root
    finally:
        os.close(descriptor)


def current(root, fingerprint=None):
    require(
        not (root / "uncertain.json").exists(),
        "Uncertain credential state refuses reuse",
    )
    pointer = json.loads(private(root / "latest.json").read_text())
    name = pointer.get("checkpoint", "")
    require(
        isinstance(name, str) and re.fullmatch(r"checkpoint-[a-f0-9]{32}", name),
        "Checkpoint locator refused",
    )
    folder = root / name
    require(
        digest(folder / "manifest.json") == pointer.get("manifest_sha256"),
        "Checkpoint pointer digest differs",
    )
    validate(folder, fingerprint)
    return folder


def begin(root, binding):
    save(
        root / "uncertain.json",
        {
            "run_id": binding["run_id"],
            "namespace": binding["namespace"],
            "phase": "issuer-run-open",
        },
    )
    sync(root)


def archive_preuse_marker(root, marker, archived):
    require(not os.path.lexists(archived), "Pre-use disposition already exists")
    os.rename(marker, archived)
    try:
        sync(root)
    except OSError:
        os.rename(archived, marker)
        sync(root)
        raise


def abort_precredential_run(root, evidence_path, inspection_path, expected):
    """Archive proven pre-restore uncertainty after a durable, hash-bound receipt."""
    root = private(Path(root), True)
    descriptor = os.open(
        root / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        private(root / "owner.lock")
        held, live = os.fstat(descriptor), (root / "owner.lock").lstat()
        require(
            (held.st_dev, held.st_ino) == (live.st_dev, live.st_ino),
            "Checkpoint owner inode changed",
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(
            digest(evidence_path) == expected["evidence_sha256"]
            and digest(inspection_path) == expected["inspection_sha256"],
            "Pre-use evidence digest differs",
        )
        evidence = json.loads(private(evidence_path).read_text())
        inspection = json.loads(private(inspection_path).read_text())
        marker = root / "uncertain.json"
        marker_bytes = private(marker).read_bytes()
        marker_stat = marker.stat()
        uncertainty = json.loads(marker_bytes)
        binding = evidence["binding"]
        run_id = expected["run_id"]
        require(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_id)
            and all(
                binding[key] == expected[key]
                for key in (
                    "run_id",
                    "namespace",
                    "source_commit",
                    "source_patch_sha256",
                )
            )
            and uncertainty["run_id"] == run_id
            and uncertainty["namespace"] == binding["namespace"]
            and uncertainty["phase"] == "issuer-run-open",
            "Pre-use run identity differs",
        )
        require(
            inspection["inspection_completed"] is True
            and inspection["uncertainty_unchanged"] is True
            and inspection["evidence_sha256"] == expected["evidence_sha256"]
            and hashlib.sha256(marker_bytes).hexdigest()
            == inspection["uncertainty_sha256_before"],
            "Pre-use marker identity differs",
        )
        provenance = inspection["input"]
        require(
            provenance["run_id"] == run_id
            and provenance["source_commit"] == binding["source_commit"]
            and provenance["original_namespace"] == binding["namespace"],
            "Pre-use inspection identity differs",
        )
        failure = evidence["failure"]
        order = evidence["activation_order"]
        require(
            failure["type"] == "GuestCheckFailure"
            and failure["frames"]
            == [
                "cmd_guest",
                "prerequisite_checks",
                "prerequisite_native_checks",
                "guest_command",
            ]
            and all(
                type(order[key]) is int
                for key in (
                    "prerequisite_checks",
                    "live_scope_for_run",
                    "live_authentication",
                )
            )
            and order["prerequisite_checks"]
            < order["live_scope_for_run"]
            < order["live_authentication"]
            and evidence["source_functions"]["cmd_guest"]["sha256"]
            == expected["preuse_control_sha256"],
            "Credential-use boundary unproven",
        )
        sources = evidence["source_files"]
        require(
            sources
            and all(
                value["matches_input_binding"] is True
                and re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
                for value in sources.values()
            ),
            "Pre-use source binding absent",
        )
        artifacts = evidence["artifact_observations"]
        require(
            all(
                artifacts[name]["exists"] is False
                and artifacts[name]["observation"] == "os.lstat ENOENT"
                for name in ("credential-scope.json", "live-home")
            )
            and evidence["fixture_facts"]["provider_checkpoint_path_in_native_fixture"]
            is False,
            "Credential restore or issuer scope may exist",
        )
        cleanup = inspection["cleanup"]
        observations = cleanup["observations"]
        require(
            cleanup["verified"] is True
            and cleanup["stop_succeeded"] is True
            and len(observations) == 2
            and all(
                item["state"] == "stopped"
                and item["instanceId"] is None
                and item["id"] == binding["namespace"]["devbox_id"]
                for item in observations
            )
            and (
                datetime.fromisoformat(observations[1]["at"])
                - datetime.fromisoformat(observations[0]["at"])
            ).total_seconds()
            >= 60,
            "Pre-use shutdown unproven",
        )
        pointer_hash = digest(root / "latest.json")
        require(
            pointer_hash == provenance["latest_pointer_sha256"],
            "Pre-use checkpoint pointer changed",
        )
        pointer = json.loads(private(root / "latest.json").read_text())
        name = pointer["checkpoint"]
        require(
            re.fullmatch(r"checkpoint-[a-f0-9]{32}", name), "Checkpoint locator refused"
        )
        seed = root / name
        manifest, _ = validate(seed)
        require(
            digest(seed / "manifest.json")
            == provenance["seed_manifest_sha256"]
            == pointer["manifest_sha256"]
            and manifest["files"] == provenance["seed_files"]
            and set(evidence["seed_files"]) == set(FILES),
            "Pre-use seed manifest changed",
        )
        for name, value in evidence["seed_files"].items():
            require(
                value["equal"] is True
                and value["guest_sha256"]
                == value["host_retained_manifest_sha256"]
                == manifest["files"][name]
                == digest(seed / name),
                "Pre-use seed bytes changed",
            )
        receipt = root / ("preuse-recovery-" + run_id + ".json")
        save(
            receipt,
            {
                "kind": "proven-precredential-abort",
                "run_id": run_id,
                "binding": binding,
                "evidence_sha256": expected["evidence_sha256"],
                "inspection_sha256": expected["inspection_sha256"],
                "latest_pointer_sha256": pointer_hash,
                "uncertainty_sha256": hashlib.sha256(marker_bytes).hexdigest(),
                "uncertainty_bytes": marker_bytes.decode(),
                "seed_files": manifest["files"],
                "shutdown": observations,
            },
        )
        sync(root)
        held, live = os.fstat(descriptor), (root / "owner.lock").lstat()
        current_marker = private(marker).stat()
        require(
            (held.st_dev, held.st_ino) == (live.st_dev, live.st_ino)
            and (marker_stat.st_dev, marker_stat.st_ino)
            == (current_marker.st_dev, current_marker.st_ino)
            and marker.read_bytes() == marker_bytes
            and digest(root / "latest.json") == pointer_hash
            and all(
                digest(seed / name) == value
                for name, value in manifest["files"].items()
            ),
            "Pre-use mutation boundary changed",
        )
        archived = root / ("preuse-aborted-" + run_id + ".json")
        archive_preuse_marker(root, marker, archived)
        return {
            "run_id": run_id,
            "receipt": str(receipt),
            "archived_marker": str(archived),
            "latest_pointer_unchanged": True,
            "seed_files_unchanged": len(FILES),
        }
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Pre-use evidence incomplete") from error
    finally:
        os.close(descriptor)


def publish(root, stage, expected_previous):
    """Previous good pointer survives failures. Uncertainty clears only after fsync."""
    manifest, accounts = validate(stage)
    require(
        (root / "uncertain.json").exists(),
        "Issuer uncertainty must precede checkpoint publication",
    )
    require(
        digest(root / "latest.json") == expected_previous,
        "Checkpoint publisher baseline changed",
    )
    pointer = json.loads(private(root / "latest.json").read_text())
    old, old_accounts = validate(root / pointer["checkpoint"])
    with (
        sqlite3.connect(
            (stage / "app.sqlite").as_uri() + "?mode=ro", uri=True
        ) as fresh,
        sqlite3.connect(
            (root / pointer["checkpoint"] / "app.sqlite").as_uri() + "?mode=ro",
            uri=True,
        ) as previous,
    ):
        old_keys = previous.execute(
            "SELECT id,key,isActive FROM apiKeys ORDER BY id"
        ).fetchall()
        new_keys = fresh.execute(
            "SELECT id,key,isActive FROM apiKeys ORDER BY id"
        ).fetchall()
        require(
            new_keys == old_keys, "Approved guest API key changed during qualification"
        )
    require(
        set(accounts) == set(old_accounts)
        and manifest["refresh_sequence"] >= old["refresh_sequence"]
        and all(
            accounts[key].get(family, 0) >= generation
            for key, generations in old_accounts.items()
            for family, generation in generations.items()
        ),
        "Stale token/account checkpoint refused",
    )
    require(stage.parent == root, "Checkpoint stage must be privately confined")
    destination = root / ("checkpoint-" + uuid.uuid4().hex)
    os.rename(stage, destination)
    sync(root)
    temporary = root / (".latest-" + uuid.uuid4().hex)
    save(
        temporary,
        {
            "checkpoint": destination.name,
            "manifest_sha256": digest(destination / "manifest.json"),
            "state": "coherent-closed-issuer-checkpoint",
            "source_run_id": manifest["run_id"],
        },
    )
    os.replace(temporary, root / "latest.json")
    sync(root)
    private(root / "uncertain.json").unlink()
    sync(root)
    return {
        "provider_counts": manifest["provider_counts"],
        "active_api_key_count": manifest["active_api_key_count"],
        "refresh_sequence": manifest["refresh_sequence"],
    }


def restore(seed, home, fingerprint):
    validate(seed, fingerprint)
    private(home, True)
    require(
        not any(character in str(home) for character in '$`"\r\n'),
        "Guest restore home contains unsafe environment characters",
    )
    state = private(home / ".9router", True)
    targets = (
        state / "db/data.sqlite",
        state / "hotswap/refresh.sqlite",
        state / "env.sh",
    )
    require(
        all(not path.exists() and not path.is_symlink() for path in targets),
        "Restore must precede initialization and worker startup",
    )
    values = environment(seed / "env.sh")
    values["DATA_DIR"] = str(state)
    for target, name in zip(targets, FILES[:3]):
        private(target.parent, True)
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as outgoing:
            if name == "env.sh":
                outgoing.write(
                    "".join(
                        f'export {key}="{value}"\n' for key, value in values.items()
                    ).encode()
                )
            else:
                with private(seed / name).open("rb") as incoming:
                    shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    for directory in (state / "db", state / "hotswap", state):
        sync(directory)


def wait_checkpoint_quiet(quiet):
    deadline = time.monotonic() + 60
    while True:
        try:
            quiet()
            return
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def export(home, scope, controller, destination, binding):
    """Drain ticks and prove zero work; freeze both databases while taking backups."""
    private(home, True)
    state = private(home / ".9router", True)
    controller.configure_qualification(scope)
    controller.control("a", "drain")

    def quiet():
        journal = controller.read_state()
        entry = journal["slots"]["a"]
        controller.verify_job("a", entry)
        value = controller.worker_status("a", entry)
        work = value.get("appWork")
        require(
            value["connections"] == 0
            and isinstance(work, dict)
            and work.get("initialized") is True
            and work.get("unknown") is False
            and work.get("draining") is True
            and all(type(work.get(key)) is int and work[key] == 0 for key in WORK),
            "Checkpoint quiescence unproven",
        )

    wait_checkpoint_quiet(quiet)
    destination.mkdir(mode=0o700)
    private(destination, True)
    databases = []
    try:
        paths = (state / "db/data.sqlite", state / "hotswap/refresh.sqlite")
        for path in paths:
            private(path)
            database = sqlite3.connect(path, timeout=5)
            database.execute("PRAGMA busy_timeout=5000")
            database.execute("BEGIN IMMEDIATE")
            databases.append(database)
        quiet()
        app, refresh = databases
        providers = dict(
            app.execute(
                "SELECT provider,count(*) FROM providerConnections GROUP BY provider"
            )
        )
        keys = app.execute("SELECT count(*) FROM apiKeys WHERE isActive=1").fetchone()[
            0
        ]
        sequence = refresh.execute(
            "SELECT value FROM refresh_sequence WHERE id=1"
        ).fetchone()[0]
        require(
            providers == {"claude": 1, "codex": 1} and keys > 0,
            "Checkpoint account population refused",
        )
        require(
            refresh.execute(
                "SELECT count(*) FROM refresh_flights WHERE state!='done'"
            ).fetchone()[0]
            == 0,
            "Uncertain issuer state refuses export",
        )
        for name, path in zip(FILES[:2], paths):
            read = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            backup = sqlite3.connect(destination / name)
            try:
                read.backup(backup)
            finally:
                backup.close()
                read.close()
            (destination / name).chmod(0o600)
        for name, path in (
            ("env.sh", state / "env.sh"),
            ("schema-manifest.json", state / "hotswap/manifest.json"),
        ):
            with (
                private(path).open("rb") as incoming,
                (destination / name).open("xb") as outgoing,
            ):
                (destination / name).chmod(0o600)
                shutil.copyfileobj(incoming, outgoing)
        manifest = {
            "schema": 1,
            "kind": "private-test-credential-seed-not-qualification",
            "credential_origin": "fresh-independently-authenticated-guest-only",
            "run_id": binding["run_id"],
            "namespace": binding["namespace"],
            "sha256": binding["sha256"],
            "source_commit": binding["source_commit"],
            "provider_counts": providers,
            "active_api_key_count": keys,
            "refresh_sequence": sequence,
            "files": {name: digest(destination / name) for name in FILES},
        }
        save(destination / "manifest.json", manifest)
        validate(destination)
        quiet()
        for path in destination.iterdir():
            with private(path).open("rb") as stream:
                os.fsync(stream.fileno())
        sync(destination)
        return {
            "export_dir": str(destination),
            "files": {
                **manifest["files"],
                "manifest.json": digest(destination / "manifest.json"),
            },
            "provider_counts": providers,
            "active_api_key_count": keys,
            "refresh_sequence": sequence,
        }
    except sqlite3.Error:
        raise ValueError("Private SQLite backup refused") from None
    finally:
        for database in databases:
            database.rollback()
            database.close()
