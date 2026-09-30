"""Exercise devbox restoration with fake launchd and isolated filesystem paths."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mac/9router_devbox_restore.sh"


class RestoreTest(unittest.TestCase):
    def test_restore_is_repeatable_and_refuses_foreign_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, state, brew, tools = (
                root / n for n in ("home", "state", "brew", "tools")
            )
            for path in (home, state, tools, brew / "bin"):
                path.mkdir(parents=True)
            release = state / "releases/v4/lib/node_modules/9router"
            release.mkdir(parents=True)
            (state / "baseline").write_text("v4\n")
            (state / "com.lfenergy.9router.plist").write_text("test plist")
            database = state / "db/data.sqlite"
            database.parent.mkdir()
            database.write_bytes(b"preserve test login state")
            (home / ".9router/runtime").mkdir(parents=True)
            for name, body in {
                "sysctl": "printf '1\\n'",
                "launchctl": 'if [ "$1" = print ]; then exit 1; fi',
            }.items():
                tool = tools / name
                tool.write_text(f"#!/bin/sh\n{body}\n")
                tool.chmod(0o755)
            script = root / "restore.sh"
            script.write_text(
                SCRIPT.read_text()
                .replace("/Volumes/devbox/9router", str(state))
                .replace("/opt/homebrew", str(brew))
            )
            env = {**os.environ, "HOME": str(home), "PATH": f"{tools}:/usr/bin:/bin"}

            def run():
                return subprocess.run(
                    ["bash", str(script)], env=env, capture_output=True, text=True
                )

            for _ in range(2):
                result = run()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((home / ".9router").resolve(), state.resolve())
                self.assertEqual(
                    (brew / "lib/node_modules/9router").resolve(), release.resolve()
                )
                self.assertEqual(database.read_bytes(), b"preserve test login state")
            self.assertTrue((state / "bootstrap-home/runtime").is_dir())
            link = brew / "lib/node_modules/9router"
            link.unlink()
            foreign = root / "foreign"
            foreign.mkdir()
            link.symlink_to(foreign)
            result = run()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected release link", result.stdout)
            self.assertEqual(link.resolve(), foreign.resolve())


if __name__ == "__main__":
    unittest.main()
