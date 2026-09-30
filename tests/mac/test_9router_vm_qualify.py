"""Run directly to check qualification model forwarding and shell quoting."""

import importlib.util
import json
import os
import shlex
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mac/9router_vm_qualify.py"
spec = importlib.util.spec_from_file_location("qualify", SCRIPT)
qualify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualify)


def test_guest_receives_model_overrides():
    models = [
        "cx/gpt-5.6-luna",
        "cc/claude-haiku-4-5-20251001",
        "cx/gpt-5.6-luna(compact_200k)",
    ]
    for probe in (None, "cx/model'; exit 9; #"):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tgz = root / "candidate.tgz"
            tgz.write_bytes(b"candidate")
            received = []

            def ssh(*argv, **kwargs):
                if "guest" in argv:
                    received.append(shlex.split(" ".join(argv)))
                    return subprocess.CompletedProcess(argv, 0, '{"result":"pass"}', "")
                return subprocess.CompletedProcess(argv, 0)

            env = {"NINEROUTER_PROBE_MODEL": probe} if probe is not None else {}
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(qualify, "QUALIFY_MODELS", models),
                patch.object(qualify, "QUALIFIED", root / "qualified"),
                patch.object(qualify, "in_vm", return_value=False),
                patch.object(qualify, "ssh", side_effect=ssh),
                patch.object(
                    qualify.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
            ):
                assert qualify.main(["run", str(tgz)]) == 0
            assert received == [
                [
                    "env",
                    "NINEROUTER_QUALIFY_MODELS=" + ",".join(models),
                    "NINEROUTER_PROBE_MODEL="
                    + (probe if probe is not None else models[0]),
                    "/opt/homebrew/bin/python3",
                    f"/tmp/9r-q-{qualify.sha256(tgz)[:12]}/9router_vm_qualify.py",
                    "guest",
                    f"/tmp/9r-q-{qualify.sha256(tgz)[:12]}/{tgz.name}",
                ]
            ]
            record = json.loads(next((root / "qualified").glob("*.json")).read_text())
            assert record["result"] == "pass"


if __name__ == "__main__":
    test_guest_receives_model_overrides()
