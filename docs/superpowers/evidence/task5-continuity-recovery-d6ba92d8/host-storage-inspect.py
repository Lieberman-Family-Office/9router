#!/usr/bin/env python3
"""Observe guest storage only, under exclusive approved lifecycle ownership."""

import fcntl
import importlib.util
import json
import os
import tempfile
import uuid
from pathlib import Path

root = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "qualifier", root / "scripts/mac/9router_vm_qualify.py"
)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
identity, name = "2tc0b2eg4mveo", "9router-qualify-recovery"
sdk = Path("/tmp").resolve() / "namespace-runtime-check-ef67fdaa"
cli = str(Path.home() / ".local/bin/devbox")
evidence = Path(__file__).parent / ("storage-inspection-" + uuid.uuid4().hex[:8])
evidence.mkdir(mode=0o700)
claims = []


def metadata(_identity):
    return q.host_metadata(identity, sdk)


def owned():
    for fd, path in claims:
        held, live = os.fstat(fd), path.lstat()
        q.require(
            (held.st_dev, held.st_ino) == (live.st_dev, live.st_ino),
            "Lifecycle claim changed",
        )


def command(*args):
    owned()
    return q.output([cli, *args], root, timeout=360)


def work():
    command("exec", name, "--", "/usr/bin/uname", "-m")
    active = metadata(identity)
    q.require(
        active["state"] == "running" and active["instance_id"],
        "Active instance identity missing",
    )
    program = (
        "import json,pathlib,sqlite3,subprocess,o"
        "s,plistlib,hashlib\nr=pathlib.Path('/Volu"
        "mes/devbox/9router');h=r/'qualification/"
        "task5-signin-5a004deda56e459b/home';b=r/"
        "'baseline.json';db=h/'.9router/db/data.s"
        "qlite'\no={'baseline_exists':b.exists(),'"
        "baseline_is_symlink':b.is_symlink(),'sig"
        "nin_home_exists':h.exists(),'database_ex"
        "ists':db.exists(),'qualification_directo"
        "ries':sorted(p.name for p in (r/'qualifi"
        "cation').iterdir()) if (r/'qualification"
        "').exists() else [],'mounts':subprocess."
        "run(['/sbin/mount'],capture_output=True,"
        "text=True,check=True).stdout,'root_listi"
        "ng':[p.name for p in r.iterdir()]}\nv=pli"
        "stlib.loads(subprocess.run(['/usr/sbin/d"
        "iskutil','info','-plist','/Volumes/devbo"
        "x'],capture_output=True,check=True).stdo"
        "ut);o['volume']={k:v.get(k) for k in ['D"
        "eviceIdentifier','VolumeUUID','APFSVolum"
        "eUUID','APFSContainerReference','TotalSi"
        "ze','VolumeFreeSpace','MountPoint','Volu"
        "meName']}\no['roots']=[{'path':str(p),'ex"
        "ists':p.exists(),'symlink':p.is_symlink("
        "),'resolved':str(p.resolve())} for p in "
        "[pathlib.Path('/Volumes/devbox'),pathlib"
        ".Path('/Users/runner/workspaces')]]\nsubj"
        "ects=[];walked=0;errors=[]\nfor base in ["
        "pathlib.Path('/Volumes/devbox'),pathlib."
        "Path('/Users/runner/workspaces')]:\n if n"
        "ot base.exists():continue\n for directory"
        ",dirs,files in os.walk(base,followlinks="
        "False,onerror=lambda e:errors.append(typ"
        "e(e).__name__)):\n  dirs[:]=[x for x in d"
        "irs if x not in ['node_modules','.git','"
        "Library','Caches','.next-cli-build','app"
        "','releases','python-tools','npm-cache']"
        "]\n  walked+=len(files)\n  if 'task5-signi"
        "n-5a004deda56e459b' in directory or 'bas"
        "eline.json' in files:\n   subjects.append"
        "({'directory':directory,'names':files,'d"
        "irectories':dirs})\n  if 'data.sqlite' in"
        " files and '/9router/' in directory:\n   "
        "p=pathlib.Path(directory)/'data.sqlite'\n"
        "   c=sqlite3.connect('file:'+str(p)+'?mo"
        "de=ro',uri=True);c.execute('pragma query"
        "_only=on')\n   try: subjects.append({'dat"
        "abase':str(p),'active_provider_counts':["
        "{'provider':x[0],'authType':x[1],'count'"
        ':x[2]} for x in c.execute("select provid'
        "er,authType,count(*) from providerConnec"
        "tions where isActive=1 and provider in ("
        "'codex','claude') group by provider,auth"
        "Type\")],'active_api_key_count':c.execute"
        "('select count(*) from apiKeys where isA"
        "ctive=1').fetchone()[0]})\n   except sqli"
        "te3.Error as e:subjects.append({'databas"
        "e':str(p),'error':type(e).__name__})\n   "
        "finally:c.close()\no['scoped_discovery']="
        "{'files_observed':walked,'errors':errors"
        ",'subjects':subjects,'excluded':['node_m"
        "odules','.git','Library','Caches','.next"
        "-cli-build','app','releases','python-too"
        "ls','npm-cache']}\nm=r/'diagnostics/stora"
        "ge-diagnostic-checkpoint.json';o['retain"
        "ed_marker_sha256']=hashlib.sha256(m.read"
        "_bytes()).hexdigest() if m.exists() else"
        " None\nprint(json.dumps(o))"
    )
    observed = json.loads(command("exec", name, "--", "python3", "-c", program))
    q.new_json(
        evidence / "storage.json",
        {
            "namespace": {"devbox_id": identity, "instance_id": active["instance_id"]},
            **observed,
        },
    )
    return {
        "guest_result": "unrun",
        "scope": "storage metadata and count-only credential presence",
        "evidence": str(evidence),
    }


try:
    for path in sorted(
        {
            Path(tempfile.gettempdir()) / ("namespace-owner-" + identity + ".lock"),
            Path("/private/tmp") / ("namespace-owner-" + identity + ".lock"),
        }
    ):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, json.dumps({
                "owner": "1c7bce1a-1309-45c3-a156-cf5623c06fda",
                "pid": os.getpid(), "scope": "Task5 storage inspection",
            }).encode())
            claims.append((fd, path))
        except BaseException:
            os.close(fd)
            raise
    with q.interruption_boundary():
        result = q.qualification_lifecycle(
            name,
            identity,
            work,
            metadata,
            lambda: None,
            stop=lambda exact: command("stop", exact, "--force"),
        )
    q.new_json(evidence / "host.json", result)
    print(json.dumps({"evidence": str(evidence), "result": result}))
    if result.get("cleanup", {}).get("verified"):
        owned()
        for fd, path in claims:
            path.unlink()
finally:
    for fd, path in claims:
        os.close(fd)
