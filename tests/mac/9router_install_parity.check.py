"""Compare real npm installation with exact qualified tarball bytes in a macOS VM."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

assert sys.platform == "darwin"
assert (
    subprocess.check_output(["sysctl", "-n", "kern.hv_vmm_present"], text=True).strip()
    == "1"
)
tarball = Path(os.environ["NINEROUTER_TEST_TARBALL"]).resolve(strict=True)
expected = {}
with tarfile.open(tarball, "r:gz") as archive:
    for member in archive.getmembers():
        parts = Path(member.name).parts
        assert parts and parts[0] == "package" and ".." not in parts
        assert not Path(member.name).is_absolute() and (
            member.isfile() or member.isdir()
        )
        if member.isfile():
            name = Path(*parts[1:]).as_posix()
            assert name not in expected
            expected[name] = hashlib.file_digest(
                archive.extractfile(member), "sha256"
            ).hexdigest()
assert expected
with tempfile.TemporaryDirectory(prefix="9router-install-parity-") as temporary:
    root = Path(temporary)
    home = root / "home"
    home.mkdir(mode=0o700)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "npm_config_cache": str(root / "cache"),
    }
    installed = subprocess.run(
        [
            "npm",
            "install",
            "-g",
            "--ignore-scripts",
            "--prefix",
            str(root / "release"),
            str(tarball),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert installed.returncode == 0, "Real npm install did not run successfully"
    release = root / "release/lib/node_modules/9router"
    actual = {}
    for path in release.rglob("*"):
        assert not path.is_symlink(), "npm installed a symlink in the qualified package"
        if path.is_file():
            actual[path.relative_to(release).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    differences = {
        "missing": sorted(expected.keys() - actual.keys()),
        "extra": sorted(actual.keys() - expected.keys()),
        "changed": sorted(
            name
            for name in expected.keys() & actual.keys()
            if expected[name] != actual[name]
        ),
    }
    counts = {name: len(paths) for name, paths in differences.items()}
    print(
        json.dumps(
            {
                "qualified_files": len(expected),
                "installed_files": len(actual),
                "differences": counts,
            }
        ),
        flush=True,
    )
    assert actual == expected, (
        "Real npm installation differs from the qualified tarball: "
        + json.dumps(counts)
    )
print(
    "PASS: real npm install preserves " + str(len(expected)) + " qualified file hashes"
)
