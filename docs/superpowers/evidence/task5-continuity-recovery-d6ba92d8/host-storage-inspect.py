#!/usr/bin/env python3
"""Observe guest storage only, under exclusive approved lifecycle ownership."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import uuid

root = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('qualifier', root / 'scripts/mac/9router_vm_qualify.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
identity, name = '2tc0b2eg4mveo', '9router-qualify-recovery'
sdk = Path('/tmp').resolve() / 'namespace-runtime-check-ef67fdaa'
cli = str(Path.home() / '.local/bin/devbox')
evidence = Path(__file__).parent / ('storage-inspection-' + uuid.uuid4().hex[:8])
evidence.mkdir(mode=0o700)
claims = []

def metadata(_identity):
    return q.host_metadata(identity, sdk)

def owned():
    for fd, path in claims:
        held, live = os.fstat(fd), path.lstat()
        q.require((held.st_dev, held.st_ino) == (live.st_dev, live.st_ino), 'Lifecycle claim changed')

def command(*args):
    owned()
    return q.output([cli, *args], root, timeout=180)

def work():
    command('exec', name, '--', '/usr/bin/uname', '-m')
    active = metadata(identity)
    q.require(active['state'] == 'running' and active['instance_id'], 'Active instance identity missing')
    program = '''import json,pathlib,sqlite3,subprocess
r=pathlib.Path('/Volumes/devbox/9router');h=r/'qualification/task5-signin-5a004deda56e459b/home';b=r/'baseline.json';db=h/'.9router/db/data.sqlite'
o={'baseline_exists':b.exists(),'baseline_is_symlink':b.is_symlink(),'signin_home_exists':h.exists(),'database_exists':db.exists(),'qualification_directories':sorted(p.name for p in (r/'qualification').iterdir()) if (r/'qualification').exists() else [],'mounts':subprocess.run(['/sbin/mount'],capture_output=True,text=True,check=True).stdout}
if b.exists():
 x=json.loads(b.read_text());o['baseline_public_identity']={k:x.get(k) for k in ['protocol','run_id','devbox_id','credential_origin','scope','source_commit','sha256']}
if db.exists():
 c=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True);o['active_provider_counts']=[{'provider':x[0],'authType':x[1],'count':x[2]} for x in c.execute("select provider,authType,count(*) from providerConnections where isActive=1 and provider in ('codex','claude') group by provider,authType")];o['active_api_key_count']=c.execute('select count(*) from apiKeys where isActive=1').fetchone()[0];c.close()
print(json.dumps(o))'''
    observed = json.loads(command('exec', name, '--', 'python3', '-c', program))
    q.new_json(evidence / 'storage.json', {'namespace': {'devbox_id': identity, 'instance_id': active['instance_id']}, **observed})
    return {'guest_result': 'unrun', 'scope': 'storage metadata and count-only credential presence', 'evidence': str(evidence)}

try:
    for path in sorted({Path(tempfile.gettempdir()) / ('namespace-owner-' + identity + '.lock'), Path('/private/tmp') / ('namespace-owner-' + identity + '.lock')}):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        claims.append((fd, path))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, json.dumps({'owner': '1c7bce1a-1309-45c3-a156-cf5623c06fda', 'pid': os.getpid(), 'scope': 'Task5 storage inspection'}).encode())
    with q.interruption_boundary():
        result = q.qualification_lifecycle(name, identity, work, metadata, lambda: None, stop=lambda exact: command('stop', exact, '--force'))
    q.new_json(evidence / 'host.json', result)
    print(json.dumps({'evidence': str(evidence), 'result': result}))
    if result.get('cleanup', {}).get('verified'):
        owned()
        for fd, path in claims:
            path.unlink()
finally:
    for fd, path in claims:
        os.close(fd)
