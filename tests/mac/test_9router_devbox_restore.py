"""Baseline/restore checks. Execute only inside an authorized Namespace guest."""

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MAC = Path(__file__).resolve().parents[2] / "scripts/mac"
spec = importlib.util.spec_from_file_location(
    "restore_qualify", MAC / "9router_vm_qualify.py"
)
qualify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualify)


class RestoreTest(unittest.TestCase):
    def test_restore_and_baseline_refusal_leave_referenced_state_untouched(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, tools, scripts = (
                root / name for name in ("home", "tools", "scripts")
            )
            for path in (home, tools, scripts):
                path.mkdir(mode=0o700)
            state = home / ".9router"
            release = state / "releases/v4/lib/node_modules/9router"
            release.mkdir(parents=True)
            (release / "package.json").write_text('{"version":"v4"}')
            (state / "db").mkdir()
            database = state / "db/data.sqlite"
            database.write_bytes(b"preserve guest login state; no real credentials")
            (state / "hotswap").mkdir()
            (state / "hotswap/state.json").write_text(
                '{"slots":{"a":{"release":"v4"}}}'
            )
            (state / "baseline").write_text("v4\n")
            (state / "referenced-release").symlink_to(release)
            for name in (
                "9router_devbox_restore.sh",
                "9router_devbox_baseline.sh",
                "9router_vm_qualify.py",
            ):
                shutil.copyfile(MAC / name, scripts / name)
            tool = tools / "sysctl"
            tool.write_text("#!/bin/sh\nprintf '1\\n'\n")
            tool.chmod(0o700)
            sentinel = tools / "unexpected-action"
            for name in ("launchctl", "npm", "ssh", "scp", "devbox"):
                tool = tools / name
                tool.write_text(
                    '#!/bin/sh\nprintf "%s\\n" "$0" >> "$ACTION_SENTINEL"\nexit 9\n'
                )
                tool.chmod(0o700)
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{tools}:{os.environ['PATH']}",
                "PYTHONDONTWRITEBYTECODE": "1",
                "ACTION_SENTINEL": str(sentinel),
            }
            before = {
                path.relative_to(state).as_posix(): path.read_bytes()
                for path in state.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            for script, args in (
                ("9router_devbox_restore.sh", ["--scope", str(root / "missing.json")]),
                (
                    "9router_devbox_baseline.sh",
                    [str(root / "missing.tgz"), "--input", str(root / "missing.json")],
                ),
            ):
                result = subprocess.run(
                    ["bash", str(scripts / script), *args],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refused", result.stderr)
                after = {
                    path.relative_to(state).as_posix(): path.read_bytes()
                    for path in state.rglob("*")
                    if path.is_file() and not path.is_symlink()
                }
                self.assertEqual(before, after)
                self.assertEqual(
                    (state / "referenced-release").resolve(), release.resolve()
                )
                self.assertFalse(
                    sentinel.exists(), "refusal invoked lifecycle or installation"
                )
                self.assertFalse((home / "Library/LaunchAgents").exists())

    def test_restore_dispatches_only_scoped_controller_without_resetting_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".9router/db/data.sqlite"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"preserve guest credentials")
            scope = root / "scope.json"
            scope.write_text(json.dumps({"phase": "qualification", "home": str(root)}))
            scope.chmod(0o600)
            with (
                patch.object(qualify, "guest_guard"),
                patch.object(qualify, "output") as controller,
            ):
                self.assertEqual(
                    qualify.cmd_restore(SimpleNamespace(scope=str(scope))), 0
                )
            controller.assert_called_once()
            argv, cwd, environment = controller.call_args.args
            self.assertEqual(argv, qualify.controller_argv(scope, "restore"))
            self.assertEqual(cwd, qualify.ROOT)
            self.assertEqual(environment["HOME"], str(root))
            self.assertEqual(database.read_bytes(), b"preserve guest credentials")

    def test_native_schema_seed_has_independent_secrets_and_never_resets_existing_state(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".9router"
            (state / "db").mkdir(parents=True)
            manifest = {
                "schemaVersion": 2,
                "migrationVersion": 3,
                "layout": [
                    {
                        "type": "table",
                        "sql": "CREATE TABLE _meta(key TEXT PRIMARY KEY,value TEXT)",
                    },
                    {
                        "type": "table",
                        "sql": "CREATE TABLE settings("
                        "id INTEGER PRIMARY KEY,data TEXT)",
                    },
                ],
            }
            with patch.dict(
                os.environ,
                {"JWT_SECRET": "must-not-copy", "INITIAL_PASSWORD": "must-not-copy"},
            ):
                qualify.initialize_baseline({"home": str(root)}, manifest)
            env = (state / "env.sh").read_bytes()
            self.assertNotIn(b"must-not-copy", env)
            self.assertIn(b"API_KEY_SECRET", env)
            database = state / "db/data.sqlite"
            with sqlite3.connect(database) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT value FROM _meta WHERE key='schemaVersion'"
                    ).fetchone(),
                    ("3",),
                )
                db.execute(
                    "INSERT INTO _meta VALUES('guest-provider-fixture','preserve')"
                )
            before = database.read_bytes()
            qualify.initialize_baseline({"home": str(root)}, manifest)
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual((state / "env.sh").read_bytes(), env)

    def test_live_authentication_without_guest_credentials_is_an_explicit_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".9router/db/data.sqlite"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE apiKeys(key TEXT,isActive INTEGER)")
                db.execute("CREATE TABLE providerConnections(isActive INTEGER)")
            with (
                patch.object(
                    qualify.urllib.request,
                    "urlopen",
                    side_effect=AssertionError("no live request"),
                ),
                self.assertRaisesRegex(
                    ValueError, "existing guest-owned live provider logins"
                ),
            ):
                qualify.live_authentication(
                    root / "scope.json",
                    {"phase": "qualification", "home": str(root)},
                    ["fixture/model"],
                )

    def test_wrapper_arguments_preserve_spaces_and_metacharacters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "9router_vm_qualify.py"
            runner.write_text("import json,sys; print(json.dumps(sys.argv[1:]))\n")
            for name, argv, expected in (
                (
                    "9router_devbox_restore.sh",
                    ["--scope", "/guest/scope ; $(never).json"],
                    ["restore", "--scope", "/guest/scope ; $(never).json"],
                ),
                (
                    "9router_devbox_baseline.sh",
                    [
                        "/guest/a package; $(never).tgz",
                        "--input",
                        "/guest/input file.json",
                    ],
                    [
                        "baseline",
                        "/guest/a package; $(never).tgz",
                        "--input",
                        "/guest/input file.json",
                    ],
                ),
            ):
                shutil.copyfile(MAC / name, root / name)
                result = subprocess.run(
                    ["bash", str(root / name), *argv],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                self.assertEqual(json.loads(result.stdout), expected)


if __name__ == "__main__":
    unittest.main()
