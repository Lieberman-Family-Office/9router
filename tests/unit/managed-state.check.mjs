// Run: node tests/unit/managed-state.check.mjs (requires Node >=22.5 for node:sqlite).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { DatabaseSync } from 'node:sqlite';
import managed from '../../src/lib/db/managed.cjs';
import { TABLES, buildCreateTableSql } from '../../src/lib/db/schema.js';
import { dedupRefresh, getRefreshWorkStatus } from '../../open-sse/services/tokenRefresh/dedup.js';
const require = createRequire(import.meta.url);
const root = fs.mkdtempSync(path.join(os.tmpdir(), '9r-state-'));
const privateDir = path.join(root, 'private');
fs.mkdirSync(privateDir, { mode: 0o700 });
const file = path.join(privateDir, 'data.sqlite');
const enrolled = path.join(privateDir, 'enrolled.json');
const refresh = path.join(privateDir, 'refresh.sqlite');
const build = path.join(root, 'hotswap-manifest.json');
const source = path.resolve(import.meta.dirname, '../..');
try {
  const manifest = await require('../../cli/scripts/build-cli.js').emitCompatibilityManifest(source, root);
  assert.deepEqual(manifest, await managed.createManifest(source));
  fs.writeFileSync(enrolled, JSON.stringify(manifest), { mode: 0o600 });
  fs.writeFileSync(file, '', { mode: 0o600 });
  const db = new DatabaseSync(file);
  for (const [name, definition] of Object.entries(TABLES)) {
    db.exec(buildCreateTableSql(name, definition));
    for (const index of definition.indexes || []) db.exec(index);
  }
  db.exec("INSERT INTO _meta VALUES('schemaVersion','1'),('backupSchemaVersion','1')");
  managed.enrollRefreshStore(refresh);
  const verify = () => managed.verifyManagedDatabase(file, build, enrolled, refresh);
  assert.equal(verify().protocol, 1);
  db.exec('ALTER TABLE settings ADD COLUMN unexpected TEXT');
  assert.throws(verify, /layout mismatch/);
  db.exec('ALTER TABLE settings DROP COLUMN unexpected');
  assert.equal(verify().protocol, 1);
  db.exec('DROP INDEX idx_pc_priority');
  assert.throws(verify, /layout mismatch/);
  db.exec(TABLES.providerConnections.indexes[2]);
  assert.equal(verify().protocol, 1);
  db.exec("UPDATE _meta SET value='2' WHERE key='schemaVersion'");
  assert.throws(verify, /metadata mismatch/);
  db.exec("UPDATE _meta SET value='1' WHERE key='schemaVersion'");
  fs.writeFileSync(enrolled, JSON.stringify({ ...manifest, persistenceFingerprint: '0'.repeat(64) }));
  assert.throws(verify, /Incompatible/);
  fs.writeFileSync(enrolled, JSON.stringify(manifest));
  fs.chmodSync(privateDir, 0o755);
  assert.throws(verify, /Unsafe/);
  fs.chmodSync(privateDir, 0o700);
  const link = path.join(root, 'private-link');
  fs.symlinkSync(privateDir, link);
  assert.throws(() => managed.privateDirectory(link), /Unsafe/);
  assert.throws(() => managed.enrollRefreshStore(refresh), /EEXIST/);
  process.env.NINEROUTER_MANAGED_WORKER = '1';
  process.env.NINEROUTER_HOTSWAP_REFRESH_DB = refresh;
  let release;
  const active = dedupRefresh('fake', 'fake-counter', async () => {
    await new Promise(resolve => { release = resolve; });
    return { accessToken: 'fake-private-value' };
  });
  assert.equal(getRefreshWorkStatus().activeRefreshOperations, 1);
  release();
  await active;
  assert.equal(getRefreshWorkStatus().activeRefreshOperations, 0);
  const store = managed.openRefreshStore(refresh);
  store.exec("UPDATE refresh_flights SET result='not-json' WHERE state='done'");
  await assert.rejects(() => dedupRefresh('fake', 'fake-counter', () => { throw new Error('must not replay'); }), /Invalid durable/);
  store.exec("CREATE TRIGGER reject_completion BEFORE UPDATE OF result ON refresh_flights BEGIN SELECT RAISE(ABORT,'fixture'); END");
  await assert.rejects(() => dedupRefresh('fake', 'fake-write', async () => ({ accessToken: 'fake-private-value' })), /Uncertain/);
  let replay = false;
  await assert.rejects(() => dedupRefresh('fake', 'fake-write', async () => { replay = true; }), /Uncertain/);
  assert.equal(replay, false);
  assert.equal(store.prepare("SELECT state FROM refresh_flights WHERE result IS NULL").get().state, 'uncertain');
  store.close();
  await assert.rejects(() => dedupRefresh('fake', '', async () => ({})), /identity/);
  delete process.env.NINEROUTER_MANAGED_WORKER;
  let calls = 0;
  const callback = async () => { calls++; return { accessToken: 'fake-unmanaged' }; };
  await Promise.all([dedupRefresh('fake', 'unmanaged', callback), dedupRefresh('fake', 'unmanaged', callback)]);
  assert.equal(calls, 1);
  // The family ceiling binds both generations to the same durable sequence.
  db.exec("INSERT INTO providerConnections VALUES('fake-id','github','oauth',NULL,NULL,1,1,'{}','now','now')");
  for (const generations of [{ oauth: 1, copilot: 999 }, { oauth: true }, { unknown: 1 }]) {
    db.prepare("UPDATE providerConnections SET data=? WHERE id='fake-id'").run(JSON.stringify({ refreshGenerations: generations }));
    assert.throws(verify, /generation store/);
  }
  db.exec("UPDATE providerConnections SET data='{}' WHERE id='fake-id'");
  // Unsupported native runtime refuses before any adapter fallback or store recreation.
  Object.defineProperty(process.versions, 'bun', { value: 'test-only', configurable: true });
  try { assert.throws(verify, /requires node:sqlite/); }
  finally { delete process.versions.bun; }
  const mirror = path.join(root, 'mirror');
  const base = path.join(mirror, 'src/lib/db');
  fs.mkdirSync(base, { recursive: true });
  for (const name of ['schema.js', 'version.js', 'migrate.js', 'migrations']) {
    fs.cpSync(path.join(source, 'src/lib/db', name), path.join(base, name), { recursive: true });
  }
  fs.writeFileSync(path.join(mirror, 'package.json'), '{"type":"module"}');
  assert.deepEqual(await managed.createManifest(mirror), manifest);
  // Every required input changes the digest. Relative path identity also changes it.
  const inputs = ['schema.js', 'version.js', 'migrate.js', ...fs.readdirSync(path.join(base, 'migrations')).map(name => `migrations/${name}`)];
  for (const name of inputs) {
    const input = path.join(base, name);
    const original = fs.readFileSync(input);
    fs.appendFileSync(input, '\n// mirror-only fingerprint mutation\n');
    assert.notEqual((await managed.createManifest(mirror)).persistenceFingerprint, manifest.persistenceFingerprint);
    fs.writeFileSync(input, original);
  }
  fs.writeFileSync(path.join(base, 'migrations', 'extra.js'), '// mirror only');
  assert.notEqual((await managed.createManifest(mirror)).persistenceFingerprint, manifest.persistenceFingerprint);
  fs.renameSync(path.join(base, 'migrations', 'extra.js'), path.join(base, 'migrations', 'renamed.js'));
  const renamed = await managed.createManifest(mirror);
  fs.renameSync(path.join(base, 'migrations', 'renamed.js'), path.join(base, 'migrations', 'extra.js'));
  assert.notEqual((await managed.createManifest(mirror)).persistenceFingerprint, renamed.persistenceFingerprint);
  fs.unlinkSync(path.join(base, 'version.js'));
  await assert.rejects(() => managed.createManifest(mirror), /ENOENT/);
  db.close();
  console.log('GREEN: layout/metadata/fingerprint refusal, private paths, durable-write failure, corrupt result, counters, family sequence ceiling, unsupported runtime, all manifest inputs and path identity');
} finally {
  delete process.env.NINEROUTER_MANAGED_WORKER;
  delete process.env.NINEROUTER_HOTSWAP_REFRESH_DB;
  fs.rmSync(root, { recursive: true, force: true });
}
