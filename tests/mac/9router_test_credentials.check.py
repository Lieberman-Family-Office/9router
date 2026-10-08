#!/usr/bin/env python3
"""Namespace-only fake restore/export dispatch proof; no actual credential paths."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

assert sys.platform == "darwin"
assert subprocess.run(["/usr/sbin/sysctl", "-n", "kern.hv_vmm_present"], capture_output=True, text=True, check=True).stdout.strip() == "1"
script = Path(__file__).resolve().parents[2] / "scripts/mac/9router_test_credentials.py"
spec = importlib.util.spec_from_file_location("checkpoint", script)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
os.umask(0o077)


def fixture(root, generation, state="done"):
    stage = root / (".stage-" + str(generation));stage.mkdir(mode=0o700)
    app = sqlite3.connect(stage / "app.sqlite")
    app.executescript("CREATE TABLE providerConnections(id TEXT,provider TEXT,authType TEXT,isActive INTEGER,data TEXT); CREATE TABLE apiKeys(id TEXT,key TEXT,isActive INTEGER); CREATE TABLE settings(id INTEGER,data TEXT);")
    for provider in ("claude", "codex"):
        app.execute("INSERT INTO providerConnections VALUES(?,?,?,?,?)", (provider+"-test",provider,"oauth",1,json.dumps({"refreshToken":"fictional-rotated-"+str(generation),"refreshGenerations":{"oauth":generation}})))
    app.execute("INSERT INTO apiKeys VALUES('fictional-id',?,1)", ("fictional-guest-key",))
    app.execute("INSERT INTO settings VALUES(1,?)", ('{"tunnelEnabled":false,"tailscaleEnabled":false,"mitmEnabled":false}',));app.commit();app.close()
    refresh=sqlite3.connect(stage/"refresh.sqlite")
    refresh.executescript("CREATE TABLE refresh_meta(id INTEGER,protocol INTEGER); INSERT INTO refresh_meta VALUES(1,1); CREATE TABLE refresh_sequence(id INTEGER,value INTEGER); CREATE TABLE refresh_flights(key TEXT,owner TEXT,state TEXT,result TEXT,generation INTEGER,started_at TEXT);")
    refresh.execute("INSERT INTO refresh_sequence VALUES(1,?)", (generation,))
    refresh.execute("INSERT INTO refresh_flights VALUES(?,?,?,?,?,?)", ("fictional-digest","fictional-owner",state,json.dumps({"accessToken":"fictional-access","refreshToken":"fictional-rotated-"+str(generation),"refreshGenerations":{"oauth":generation}}),generation,"fictional-time"));refresh.commit();refresh.close()
    (stage/"env.sh").write_text('export DATA_DIR="/fictional/test/.9router"\nexport JWT_SECRET="fictional-jwt"\nexport API_KEY_SECRET="fictional-api"\nexport MACHINE_ID_SALT="fictional-salt"\nexport INITIAL_PASSWORD="fictional-password"\nexport NODE_ENV="production"\nexport REQUEST_DETAILS_MODE="disabled"\n')
    (stage/"schema-manifest.json").write_text('{"protocol":1,"persistenceFingerprint":"fictional-schema"}')
    for name in c.FILES:(stage/name).chmod(0o600)
    c.save(stage/"manifest.json", {"schema":1,"kind":"private-test-credential-seed-not-qualification","credential_origin":"fresh-independently-authenticated-guest-only","provider_counts":{"claude":1,"codex":1},"active_api_key_count":1,"refresh_sequence":generation,"run_id":"fictional","files":{name:c.digest(stage/name) for name in c.FILES}})
    return stage


def refused(fn):
    try:fn()
    except ValueError:return
    raise AssertionError("Expected refusal")


with tempfile.TemporaryDirectory(prefix="credential-check-", dir=str(Path.home())) as temporary:
    root=Path(temporary).resolve()
    with c.owner(root):
        refused(lambda:c.owner(root).__enter__())
        seed=fixture(root,1)
        destination=root/("checkpoint-"+"a"*32);os.rename(seed,destination)
        c.save(root/"latest.json", {"checkpoint":destination.name,"manifest_sha256":c.digest(destination/"manifest.json")})
        previous=c.digest(root/"latest.json")
        c.begin(root,{"run_id":"fictional-run","namespace":{"devbox_id":"fake","instance_id":"fake"}})
        refused(lambda:c.current(root))
        stage=fixture(root,2)
        with patch.object(c.os,"replace",side_effect=OSError("fictional publish failure")):
            try:c.publish(root,stage,previous)
            except OSError:pass
            else:raise AssertionError("Failed export passed")
        assert c.digest(root/"latest.json")==previous and (root/"uncertain.json").exists()
        stage=fixture(root,3)
        summary=c.publish(root,stage,previous)
        assert summary["refresh_sequence"]==3 and not (root/"uncertain.json").exists()
        seed=c.current(root,"fictional-schema")
        home=root/"home";home.mkdir(mode=0o700)
        for name in (".9router",".9router/db",".9router/hotswap"):(home/name).mkdir(mode=0o700)
        c.restore(seed,home,"fictional-schema")
        app=sqlite3.connect(home/".9router/db/data.sqlite")
        assert all(json.loads(row[0])["refreshToken"]=="fictional-rotated-3" for row in app.execute("SELECT data FROM providerConnections"));app.close()
        refresh=sqlite3.connect(home/".9router/hotswap/refresh.sqlite")
        assert refresh.execute("SELECT value FROM refresh_sequence").fetchone()[0]==3
        assert json.loads(refresh.execute("SELECT result FROM refresh_flights").fetchone()[0])["refreshToken"]=="fictional-rotated-3";refresh.close()
        assert 'export DATA_DIR="'+str(home/".9router")+'"' in (home/".9router/env.sh").read_text()
        refused(lambda:c.restore(seed,home,"fictional-schema"));refused(lambda:c.current(root,"wrong-schema"))
        # Enter the actual final export dispatch using fictional private worker state.
        c.save(home/".9router/hotswap/manifest.json", json.loads((seed/"schema-manifest.json").read_text()))
        class Controller:
            def configure_qualification(self, scope):assert scope==root/"scope.json"
            def control(self, slot, op):assert (slot,op)==("a","drain")
            def read_state(self):return {"slots":{"a":{"release":"fictional"}}}
            def verify_job(self, slot, entry):assert slot=="a"
            def worker_status(self, slot, entry):return {"connections":0,"appWork":{"initialized":True,"unknown":False,"draining":True,**dict.fromkeys(c.WORK,0)}}
        exported=root/"exported"
        c.export(home,root/"scope.json",Controller(),exported,{"run_id":"fictional-final","namespace":{"devbox_id":"fake","instance_id":"fake"},"sha256":"a"*64,"source_commit":"b"*40})
        checked,account_generations=c.validate(exported,"fictional-schema")
        assert checked["refresh_sequence"]==3 and all(value["oauth"]==3 for value in account_generations.values())
        c.begin(root,{"run_id":"fictional-next","namespace":{}})
        stale=fixture(root,0);refused(lambda:c.publish(root,stale,c.digest(root/"latest.json")))
        assert (root/"uncertain.json").exists()
        uncertain=fixture(root,4,"uncertain");refused(lambda:c.publish(root,uncertain,c.digest(root/"latest.json")))
    refused(lambda:c.owner(root).__enter__())
print("PASS: private fake latest-token/generation restore; second owner, schema/stale/uncertain state and failed export refuse; previous checkpoint preserved")
