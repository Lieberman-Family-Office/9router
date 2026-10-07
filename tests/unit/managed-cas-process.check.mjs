import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fork } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import managed from '../../src/lib/db/managed.cjs';
import { TABLES, buildCreateTableSql } from '../../src/lib/db/schema.js';
const self = import.meta.filename;
if (process.argv[2] === 'child') {
  const { updateProviderConnection, getProviderConnectionById, createProviderConnection } = await import('../../src/lib/db/repos/connectionsRepo.js');
  const generation = Number(process.argv[3]);
  if (generation < 0) {
    if (generation === -1) {
      await createProviderConnection({ provider: 'github', authType: 'oauth', email: 'fake@example.invalid',
        accessToken: 'fake-reauth', refreshToken: 'fake-reauth-refresh' });
    } else {
      const { dedupRefresh } = await import('../../open-sse/services/tokenRefresh/dedup.js');
      const result = await dedupRefresh('github', generation === -2 ? 'fake-old-refresh' : 'fake-reauth-refresh', async () => {
        if (generation === -2) {
          process.send({ op: 'pending' });
          await new Promise(resolve => process.once('message', resolve));
        }
        return { accessToken: generation === -2 ? 'fake-delayed' : 'fake-next', refreshToken: 'fake-rotated', expiresIn: 3600 };
      });
      await updateProviderConnection('fake-id', result);
    }
    global._dbAdapter?.instance?.close();
    process.exit(0);
  }
  const family = process.argv[4] || 'oauth';
  await updateProviderConnection('fake-id', generation ? (family === 'oauth'
    ? { accessToken: `fake-${generation}`, expiresAt: `expiry-${generation}` }
    : { providerSpecificData: { copilotToken: `fake-${generation}`, copilotTokenExpiresAt: generation } })
    : { providerSpecificData: { usageMarker: 'fake-usage' } }, generation ? { [family]: generation } : undefined);
  if (generation) {
    await assert.rejects(() => updateProviderConnection('fake-id', { accessToken: 'fake-ungated' }), /generation CAS/);
    await assert.rejects(() => updateProviderConnection('fake-id', { accessToken: 'fake-invalid' }, true), /generation/);
  }
  const row = await getProviderConnectionById('fake-id');
  assert.ok(row);
  global._dbAdapter?.instance?.close();
} else {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), '9r-cas-'));
  const directory = path.join(root, 'db');
  fs.mkdirSync(directory, { mode: 0o700 });
  const file = path.join(directory, 'data.sqlite');
  fs.writeFileSync(file, '', { mode: 0o600 });
  const db = new DatabaseSync(file);
  const children = new Set();
  try {
    db.exec('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;');
    for (const [name, definition] of Object.entries(TABLES)) {
      db.exec(buildCreateTableSql(name, definition));
      for (const index of definition.indexes || []) db.exec(index);
    }
    db.exec("INSERT INTO _meta VALUES('schemaVersion','1'),('backupSchemaVersion','1')");
    db.exec("INSERT INTO providerConnections VALUES('fake-id','codex','oauth',NULL,NULL,1,1,'{}','now','now')");
    const manifest = await managed.createManifest(path.resolve(import.meta.dirname, '../..'));
    const receipt = path.join(directory, 'enrolled.json');
    fs.writeFileSync(receipt, JSON.stringify(manifest), { mode: 0o600 });
    const refresh = path.join(directory, 'refresh.sqlite');
    managed.enrollRefreshStore(refresh, 2);
    function child(generation, expectFailure = false, family = 'oauth', onPending) {
      return new Promise((resolve, reject) => {
        const proc = fork(self, ['child', String(generation), family], {
          execArgv: ['--loader', path.join(import.meta.dirname, 'managed-imports.loader.mjs')],
          env: { ...process.env, HOME: root, DATA_DIR: root,
            NINEROUTER_MANAGED_WORKER: '1', NINEROUTER_HOTSWAP_MANIFEST: receipt,
            NINEROUTER_HOTSWAP_ENROLLED_MANIFEST: receipt, NINEROUTER_HOTSWAP_REFRESH_DB: refresh },
          stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
        });
        children.add(proc);
        proc.on('message', message => { if (message.op === 'pending') onPending?.(proc); });
        let output = '';
        proc.stdout.on('data', data => { output += data; });
        proc.stderr.on('data', data => { output += data; });
        const deadline = setTimeout(() => { proc.kill(); reject(new Error('CAS child deadline')); }, 10000);
        proc.on('error', reject);
        proc.on('exit', code => {
          children.delete(proc);
          clearTimeout(deadline);
          if (output.includes('fake-ungated') || output.includes('fake-invalid')) return reject(new Error('Credential output forbidden'));
          if (expectFailure ? code === 0 : code !== 0) return reject(new Error(`CAS child failed (exit ${code}): ${output.replace(/fake-[\w-]+/g, '[redacted]')}`));
          if (expectFailure && !output.includes('Enrolled database layout mismatch')) return reject(new Error('CAS child did not refuse the mutated layout'));
          resolve();
        });
      });
    }
    for (const family of ['oauth', 'copilot']) {
      for (const order of [[1, 2], [2, 1]]) {
        db.exec("UPDATE providerConnections SET data='{}' WHERE id='fake-id'");
        for (const generation of order) await child(generation, false, family);
        const row = JSON.parse(db.prepare("SELECT data FROM providerConnections WHERE id='fake-id'").get().data);
        assert.ok((family === 'oauth' ? row.accessToken : row.providerSpecificData.copilotToken) === 'fake-2');
        assert.equal(row.refreshGenerations[family], 2);
      }
    }
    for (const order of [['oauth', 'copilot'], ['copilot', 'oauth']]) {
      db.exec("UPDATE providerConnections SET data='{}' WHERE id='fake-id'");
      for (const family of order) await child(family === 'oauth' ? 1 : 2, false, family);
      const row = JSON.parse(db.prepare("SELECT data FROM providerConnections WHERE id='fake-id'").get().data);
      assert.ok(row.accessToken === 'fake-1' && row.providerSpecificData.copilotToken === 'fake-2');
      assert.deepEqual(row.refreshGenerations, { oauth: 1, copilot: 2 });
      assert.equal(row.expiresAt, 'expiry-1');
      assert.equal(row.providerSpecificData.copilotTokenExpiresAt, 2);
    }
    db.exec("UPDATE providerConnections SET data='{}' WHERE id='fake-id'");
    await Promise.all([child(2), child(0)]);
    const row = JSON.parse(db.prepare("SELECT data FROM providerConnections WHERE id='fake-id'").get().data);
    assert.equal(row.accessToken, 'fake-2');
    assert.ok(row.providerSpecificData.usageMarker === 'fake-usage');
    db.prepare("UPDATE providerConnections SET provider='github',email='fake@example.invalid',data=? WHERE id='fake-id'")
      .run(JSON.stringify({ accessToken: 'fake-old', refreshToken: 'fake-old-refresh' }));
    let announce;
    const pending = new Promise(resolve => { announce = resolve; });
    const delayed = child(-2, false, 'oauth', announce);
    const oldWorker = await Promise.race([pending, delayed.then(() => { throw new Error('CAS child exited without pending announcement'); })]);
    await child(-1);
    oldWorker.send({ op: 'finish' });
    await delayed;
    assert.equal(JSON.parse(db.prepare("SELECT data FROM providerConnections WHERE id='fake-id'").get().data).accessToken,
      'fake-reauth', 'pending old refresh must not overwrite a reauthorized grant');
    await child(-3);
    assert.equal(JSON.parse(db.prepare("SELECT data FROM providerConnections WHERE id='fake-id'").get().data).accessToken, 'fake-next');
    db.exec('ALTER TABLE settings ADD COLUMN incompatible TEXT');
    await child(0, true);
    assert.ok(db.prepare('PRAGMA table_info(settings)').all().some(row => row.name === 'incompatible'));
    console.log('GREEN: actual repo OAuth/Copilot CAS both same-family and crossed orders, nested usage contention, pending refresh/reauth fencing, new-grant refresh, readiness refuses mutated DB');
  } finally {
    for (const proc of children) proc.kill('SIGTERM');
    db.close();
    fs.rmSync(root, { recursive: true, force: true });
  }
}
