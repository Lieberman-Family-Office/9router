"""Task 2 templates are parsed and rendered with plistlib, never XML substitution."""

import pathlib
import plistlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_templates():
    for kind in ("worker", "proxy"):
        template = ROOT / f"scripts/mac/templates/com.lfenergy.9router-{kind}.plist"
        data = plistlib.loads(template.read_bytes())
        assert data["KeepAlive"] is True
        assert data["ThrottleInterval"] == 5
        values = {
            "__SLOT__": "a",
            "__NODE__": "/isolated/node",
            "__CADDY__": "/isolated/caddy",
            "__WRAPPER__": "/isolated/worker.cjs",
            "__CONFIG__": "/isolated/a&b/config",
            "__STDOUT__": f"/isolated/{kind}.out.log",
            "__STDERR__": f"/isolated/{kind}.err.log",
            "__HOME__": "/isolated/home",
        }
        data["Label"] = data["Label"].replace("__SLOT__", values["__SLOT__"])
        data["ProgramArguments"] = [values.get(v, v) for v in data["ProgramArguments"]]
        for key in ("StandardOutPath", "StandardErrorPath"):
            data[key] = values[data[key]]
        if kind == "worker":
            data["EnvironmentVariables"]["HOME"] = values["__HOME__"]
        assert b"__" not in plistlib.dumps(data), "unresolved template placeholder"
        assert plistlib.loads(plistlib.dumps(data)) == data
        assert data["Label"] == f"com.lfenergy.9router-{kind}" + (
            "-a" if kind == "worker" else ""
        )
        assert data["ProgramArguments"][0].startswith("/")
        assert data["StandardOutPath"] != data["StandardErrorPath"]


if __name__ == "__main__":
    test_templates()
    print(
        "PASS: worker/proxy plistlib rendering, concrete paths, labels, "
        "KeepAlive and isolated logs"
    )
