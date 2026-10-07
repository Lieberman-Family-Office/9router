#!/usr/bin/env python3
"""Compare scoped root/workspace checkpoint bytes before and after exact guest stop."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

source = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('qualifier', source / 'scripts/mac/9router_vm_qualify.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
identity, name = '2tc0b2eg4mveo', '9router-qualify-recovery'
sdk = Path('/tmp').resolve() / 'namespace-runtime-check-ef67fdaa'
cli = str(Path.home() / '.local/bin/devbox')
run_id = 'boundary-' + uuid.uuid4().hex[:12]
local = Path(__file__).parent / run_id
local.mkdir(mode=0o700)
claims = []

program = '''import hashlib,json,os,pathlib,plistlib,subprocess,sys,time
root=pathlib.Path('/Volumes/devbox');workspace=pathlib.Path('/Users/runner/workspaces').resolve()
paths=[root/(sys.argv[1]+'.json'),workspace/(sys.argv[1]+'.json'),root/'9router/diagnostics'/sys.argv[1]/'home/db/checkpoint.json']
phase=sys.argv[2]
observations=[]
if phase=='create':
 for p in paths:
  assert not p.exists();p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  with p.open('x') as f:json.dump({'purpose':'noncredential root/workspace durability check','id':sys.argv[1]},f);f.flush();os.fsync(f.fileno())
  p.chmod(0o600)
  for parent in [p.parent,root]:
   fd=os.open(parent,os.O_RDONLY);os.fsync(fd);os.close(fd)
 subprocess.run(['/bin/sync'],check=True)
 for index in range(3):
  observations.append({'at':time.time(),'hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}});time.sleep(2)
volume=plistlib.loads(subprocess.run(['/usr/sbin/diskutil','info','-plist',str(root)],capture_output=True,check=True).stdout)
result={'phase':phase,'at':time.time(),'workspace_resolved':str(workspace),'volume_uuid':volume.get('VolumeUUID'),'device':volume.get('DeviceIdentifier'),'paths':[{'path':str(p),'exists':p.exists(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None,'device':p.stat().st_dev if p.exists() else None} for p in paths],'within_process':observations,'hostname':subprocess.run(['/bin/hostname'],capture_output=True,text=True,check=True).stdout.strip()}
if phase=='create':
 services=[]
 for directory in ['/Library/LaunchDaemons','/Library/LaunchAgents','/Users/runner/Library/LaunchAgents']:
  d=pathlib.Path(directory)
  if not d.exists():continue
  for p in d.glob('*.plist'):
   if not any(x in p.name.lower() for x in ['namespace','devbox','9router']):continue
   v=plistlib.loads(p.read_bytes());args=v.get('ProgramArguments',[]);services.append({'file':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'label':v.get('Label'),'program':v.get('Program'),'argument_executable':args[0] if args else None,'arguments_count':len(args),'run_at_load':v.get('RunAtLoad'),'keep_alive':v.get('KeepAlive'),'working_directory':v.get('WorkingDirectory')})
 result['scoped_startup_services']=services
 surface=[]
 candidates=[pathlib.Path('/Library/LaunchDaemons/so.namespace.vmguest.plist'),root/'9router/start.sh',root/'9router/task5-dashboard-guest.py']
 for item in services:
  plist=pathlib.Path(item['file']);v=plistlib.loads(plist.read_bytes());args=v.get('ProgramArguments',[])
  if len(args)>1 and args[0] in ['/bin/bash','/bin/sh']:candidates.append(pathlib.Path(args[1]))
 for p in candidates:
  if not p.is_file():continue
  text=p.read_text(errors='replace');safe=[{'line':i+1,'text':line[:240]} for i,line in enumerate(text.splitlines()) if any(word in line.lower() for word in ['rm ','delete','unlink','rmtree','rsync','restore','snapshot','mount','apfs','sync','9router','workspace','devbox']) and not any(word in line.lower() for word in ['token','password','secret','credential','authorization','cookie','bearer'])]
  surface.append({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'line_count':len(text.splitlines()),'scoped_relevant_lines':safe})
 result['startup_source_observation']=surface
print(json.dumps(result))'''

def metadata(_identity):
    return q.host_metadata(identity, sdk)

def owned():
    for fd, path in claims:
        a, b = os.fstat(fd), path.lstat()
        q.require((a.st_dev, a.st_ino) == (b.st_dev, b.st_ino), 'Lifecycle claim changed')

def command(*args):
    owned()
    return q.output([cli, *args], source, timeout=360)

def api_snapshot(label):
    target = local / (label + '-api.json')
    result = subprocess.run(['node', str(Path(__file__).parent / 'snapshot-metadata.mjs'), str(sdk), str(target)], cwd=source, capture_output=True, text=True, timeout=120)
    q.require(result.returncode == 0 and target.is_file(), 'Read-only lineage collection failed')

def work(phase):
    command('exec', name, '--', '/usr/bin/uname', '-m')
    active = metadata(identity)
    api_snapshot(phase + '-before')
    observed = json.loads(command('exec', name, '--', 'python3', '-c', program, run_id, phase))
    q.new_json(local / (phase + '.json'), {'namespace': active, **observed})
    if phase == 'create':
        again = json.loads(command('exec', name, '--', 'python3', '-c', program, run_id, 'same-instance-read'))
        q.require(metadata(identity)['instance_id'] == active['instance_id'], 'Instance changed during running byte comparison')
        q.new_json(local / 'same-instance-read.json', {'namespace': active, **again})
    api_snapshot(phase + '-after-work')
    return {'guest_result': 'unrun', 'scope': 'root/workspace checkpoint persistence and scoped startup metadata only', 'namespace': active}

try:
    for path in sorted({Path(tempfile.gettempdir()) / ('namespace-owner-' + identity + '.lock'), Path('/private/tmp') / ('namespace-owner-' + identity + '.lock')}):
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600);claims.append((fd,path));fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);os.write(fd,json.dumps({'owner':'1c7bce1a-1309-45c3-a156-cf5623c06fda','pid':os.getpid(),'scope':'Task5 root/workspace boundary comparison'}).encode())
    for phase in ['create','read']:
        with q.interruption_boundary():
            result=q.qualification_lifecycle(name,identity,lambda:work(phase),metadata,lambda:None,stop=lambda exact:command('stop',exact,'--force'))
        q.new_json(local/(phase+'-host.json'),result)
        q.require(result.get('cleanup',{}).get('verified') and not result.get('failure'),'Guest observation or cleanup incomplete')
        api_snapshot(phase+'-after-stop')
    print(json.dumps({'evidence':str(local),'run_id':run_id}))
    owned()
    for fd,path in claims:path.unlink()
finally:
    for fd,path in claims:os.close(fd)
