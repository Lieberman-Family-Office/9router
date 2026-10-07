#!/usr/bin/env python3
"""Prove new guest-only nested checkpoints survive stop; no credential access."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import uuid

source = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('qualifier', source / 'scripts/mac/9router_vm_qualify.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
identity, name = '2tc0b2eg4mveo', '9router-qualify-recovery'
sdk = Path('/tmp').resolve() / 'namespace-runtime-check-ef67fdaa'
cli = str(Path.home() / '.local/bin/devbox')
run_id = 'checkpoint-' + uuid.uuid4().hex[:12]
local = Path(__file__).parent / run_id
local.mkdir(mode=0o700)
claims = []

def metadata(_identity):
    return q.host_metadata(identity, sdk)

def command(*args):
    for fd, path in claims:
        held, live = os.fstat(fd), path.lstat()
        q.require((held.st_dev, held.st_ino) == (live.st_dev, live.st_ino), 'Lifecycle claim changed')
    return q.output([cli, *args], source, timeout=360)

program = '''import hashlib,json,os,pathlib,sqlite3,subprocess,sys,plistlib
root=pathlib.Path('/Volumes/devbox/9router/diagnostics')/sys.argv[1]
phase=sys.argv[2]
if phase=='create':
 root.mkdir(mode=0o700)
 home=root/'home';home.mkdir(mode=0o700);dbdir=home/'db';dbdir.mkdir(mode=0o700)
 p=dbdir/'data.sqlite';c=sqlite3.connect(p);c.execute('create table checkpoint(id integer primary key, purpose text)');c.execute("insert into checkpoint values(1,'noncredential durability fixture')");c.commit();c.close();p.chmod(0o600)
 marker=root/'binding.json'
 with marker.open('x') as f:json.dump({'run_id':sys.argv[1],'purpose':'noncredential durability fixture'},f);f.flush();os.fsync(f.fileno())
 marker.chmod(0o600)
 for path in [p,marker,dbdir,home,root,root.parent]:
  fd=os.open(path,os.O_RDONLY);os.fsync(fd);os.close(fd)
 subprocess.run(['/bin/sync'],check=True)
paths=[root,root/'binding.json',root/'home/db/data.sqlite']
result={'phase':phase,'paths':[{'path':str(p),'exists':p.exists(),'device':p.stat().st_dev if p.exists() else None} for p in paths]}
if all(p.exists() for p in paths):
 result['hashes']={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths[1:]}
 c=sqlite3.connect('file:'+str(paths[2])+'?mode=ro',uri=True);result['rows']=c.execute('select count(*) from checkpoint').fetchone()[0];c.close()
v=plistlib.loads(subprocess.run(['/usr/sbin/diskutil','info','-plist','/Volumes/devbox'],capture_output=True,check=True).stdout);result['volume_uuid']=v.get('VolumeUUID')
print(json.dumps(result))'''

def work(phase):
    command('exec', name, '--', '/usr/bin/uname', '-m')
    active = metadata(identity)
    observed = json.loads(command('exec', name, '--', 'python3', '-c', program, run_id, phase))
    q.new_json(local / (phase + '.json'), {'namespace': active, **observed})
    return {'guest_result': 'unrun', 'scope': 'new nested checkpoint persistence only', 'namespace': active}

try:
    for path in sorted({Path(tempfile.gettempdir()) / ('namespace-owner-' + identity + '.lock'), Path('/private/tmp') / ('namespace-owner-' + identity + '.lock')}):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600);claims.append((fd, path));fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB);os.write(fd,json.dumps({'owner':'1c7bce1a-1309-45c3-a156-cf5623c06fda','pid':os.getpid(),'scope':'Task5 harmless nested durability proof'}).encode())
    for phase in ['create', 'read']:
        with q.interruption_boundary():
            result=q.qualification_lifecycle(name, identity, lambda: work(phase), metadata, lambda:None, stop=lambda exact:command('stop',exact,'--force'))
        q.new_json(local / (phase + '-host.json'), result)
        q.require(result.get('cleanup',{}).get('verified') and not result.get('failure'), 'Checkpoint observation or shutdown incomplete')
    before=q.private_json(local/'create.json');after=q.private_json(local/'read.json')
    success=before.get('hashes')==after.get('hashes') and bool(after.get('hashes')) and after.get('rows')==1 and before['volume_uuid']==after['volume_uuid']
    q.new_json(local/'outcome.json',{'success':success,'run_id':run_id,'scope':'noncredential nested SQLite and marker retained after exact guest stop/reactivation','production_touched':False})
    print(json.dumps({'evidence':str(local),'success':success}))
    for fd,path in claims:path.unlink()
finally:
    for fd,path in claims:os.close(fd)
