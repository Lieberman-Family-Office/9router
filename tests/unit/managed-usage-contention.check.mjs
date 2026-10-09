// Run in Namespace: node --loader ./tests/unit/managed-imports.loader.mjs tests/unit/managed-usage-contention.check.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fork } from 'node:child_process';
import { once } from 'node:events';
import { DatabaseSync } from 'node:sqlite';
import managed from '../../src/lib/db/managed.cjs';
import { TABLES, buildCreateTableSql } from '../../src/lib/db/schema.js';

if (process.argv[2] === 'child') {
  const { getAdapter } = await import('../../src/lib/db/driver.js');
  const { saveRequestUsage } = await import('../../src/lib/db/repos/usageRepo.js');
  const db = await getAdapter();
  const entry = { timestamp: '2026-10-09T12:00:00.000Z', provider: null, model: 'fixture',
    connectionId: 'fake-connection', apiKey: 'fake-api-key', tokens: { prompt_tokens: 7, completion_tokens: 3 } };
  let heartbeatResolve;
  const errors = [];
  const heartbeat = new Promise(resolve => { heartbeatResolve = resolve; });
  console.error = (message, error) => { errors.push({ message, code: error?.code, errcode: error?.errcode }); };
  process.send({ op: 'ready' });
  process.on('message', async message => {
    if (message.op === 'heartbeat') {
      process.send({ op: 'heartbeat', id: message.id }, heartbeatResolve);
      return;
    }
    if (message.op !== 'save') throw new Error('Unknown usage check command');
    if (message.fail) {
      const run = db.run.bind(db);
      db.run = (sql, params) => {
        if (sql.startsWith('INSERT INTO usageDaily')) throw new Error('Injected daily-write failure');
        return run(sql, params);
      };
    }
    if (message.outer) {
      db.exec('BEGIN IMMEDIATE');
      db.run("INSERT INTO _meta VALUES('outer-owned','preserve')");
    }
    const started = performance.now();
    await saveRequestUsage({ ...entry });
    if (!message.fail && !message.outer) await saveRequestUsage({ ...entry });
    if (message.heartbeat) await heartbeat;
    const result = { op: 'result', work: { ...managed.workState() }, errors,
      elapsed_ms: performance.now() - started,
      history: db.all('SELECT promptTokens,completionTokens FROM usageHistory'),
      daily: db.all('SELECT data FROM usageDaily').map(row => JSON.parse(row.data)),
      lifetime: db.get("SELECT value FROM _meta WHERE key='totalRequestsLifetime'")?.value ?? null,
      outer: db.get("SELECT value FROM _meta WHERE key='outer-owned'")?.value ?? null,
      transaction_open: db.raw.isTransaction };
    if (message.outer) db.exec('ROLLBACK');
    process.send(result, () => { db.close(); process.disconnect(); });
  });
} else {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), '9r-usage-'));
  fs.chmodSync(root, 0o700);
  const children = new Set();
  const reports = [];
  async function scenario(name, options = {}) {
    const home = path.join(root, name), directory = path.join(home, 'db');
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const file = path.join(directory, 'data.sqlite');
    fs.closeSync(fs.openSync(file, 'wx', 0o600));
    const db = new DatabaseSync(file);
    let child, releaseTimer, heartbeatTimer;
    try {
      db.exec('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000');
      for (const [table, definition] of Object.entries(TABLES)) {
        db.exec(buildCreateTableSql(table, definition));
        for (const index of definition.indexes || []) db.exec(index);
      }
      db.exec("INSERT INTO _meta VALUES('schemaVersion','1'),('backupSchemaVersion','1')");
      const manifest = path.join(home, 'enrolled.json');
      fs.writeFileSync(manifest, JSON.stringify(await managed.createManifest(path.resolve(import.meta.dirname, '../..'))), { mode: 0o600 });
      const refresh = path.join(home, 'refresh.sqlite');
      managed.enrollRefreshStore(refresh, 2);
      child = fork(import.meta.filename, ['child'], {
        execArgv: ['--loader', path.join(import.meta.dirname, 'managed-imports.loader.mjs')],
        env: { ...process.env, HOME: home, DATA_DIR: home, NINEROUTER_MANAGED_WORKER: '1',
          NINEROUTER_HOTSWAP_MANIFEST: manifest, NINEROUTER_HOTSWAP_ENROLLED_MANIFEST: manifest,
          NINEROUTER_HOTSWAP_REFRESH_DB: refresh }, stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
      });
      children.add(child);
      let output = '', readyResolve, resultResolve, exitResolve, reject;
      const failed = new Promise((_, fail) => { reject = fail; });
      const ready = new Promise(resolve => { readyResolve = resolve; });
      const result = new Promise(resolve => { resultResolve = resolve; });
      const exited = new Promise(resolve => { exitResolve = resolve; });
      const timeout = setTimeout(() => reject(new Error('Usage check child deadline')), 15000);
      child.stdout.on('data', chunk => { output += chunk; });
      child.stderr.on('data', chunk => { output += chunk; });
      child.on('error', reject);
      child.on('exit', code => { clearTimeout(timeout); children.delete(child); exitResolve(code);
        if (code !== 0) reject(new Error(`Usage check child failed (${code}): ${output}`)); });
      let heartbeatAt = null, heartbeatSent = null;
      child.on('message', message => {
        if (message.op === 'ready') readyResolve();
        if (message.op === 'result') resultResolve(message);
        if (message.op === 'heartbeat' && message.id === name) heartbeatAt = performance.now();
      });
      await Promise.race([ready, failed]);
      if (options.hold) {
        // A positively owns the writer lock before B enters the real usage path.
        db.exec('BEGIN IMMEDIATE');
        db.prepare("INSERT INTO _meta VALUES('writer-barrier','held')").run();
        releaseTimer = setTimeout(() => db.exec('COMMIT'), options.hold);
        heartbeatTimer = setTimeout(() => {
          heartbeatSent = performance.now();
          if (child.connected) child.send({ op: 'heartbeat', id: name });
        }, 50);
      }
      child.send({ op: 'save', fail: options.fail === true, outer: options.outer === true, heartbeat: Boolean(options.hold) });
      const observed = await Promise.race([result, failed]);
      assert.equal(await Promise.race([exited, failed]), 0);
      if (options.hold) assert.notEqual(heartbeatAt, null, 'Lock-wait IPC observation must run');
      reports.push({ name, elapsed_ms: observed.elapsed_ms,
        heartbeat_delay_ms: heartbeatAt === null || heartbeatSent === null ? null : heartbeatAt - heartbeatSent,
        errors: observed.errors, unknown: observed.work.unknown, persistence: observed.work.persistence });
      assert.equal(observed.work.persistence, 0, 'Usage persistence work must settle');
      if (options.fail) {
        assert.equal(observed.work.unknown, true, 'A real persistence failure must stay unknown');
        assert.equal(observed.errors.length, 1);
        assert.deepEqual(observed.history, []);
        assert.deepEqual(observed.daily, []);
        assert.equal(observed.lifetime, null, 'Failed usage must not partly commit');
        assert.equal(observed.transaction_open, false, 'Failed owned transaction must roll back');
      } else if (options.outer) {
        assert.equal(observed.work.unknown, true);
        assert.equal(observed.outer, 'preserve', 'A failed acquisition must not roll back another transaction');
        assert.equal(observed.transaction_open, true);
        assert.deepEqual(observed.history, []);
      } else {
        assert.equal(observed.work.unknown, false, 'Contended managed usage must commit without losing accounting');
        assert.deepEqual(observed.errors, []);
        assert.deepEqual(observed.history, [{ promptTokens: 7, completionTokens: 3 }]);
        assert.equal(observed.daily.length, 1);
        assert.equal(observed.daily[0].requests, 1, 'Duplicate usage must not double count');
        assert.equal(observed.daily[0].promptTokens, 7);
        assert.equal(observed.daily[0].completionTokens, 3);
        assert.equal(observed.lifetime, '1');
        assert.equal(observed.transaction_open, false);
      }
    } finally {
      clearTimeout(releaseTimer); clearTimeout(heartbeatTimer);
      if (db.isTransaction) db.exec('ROLLBACK');
      db.close();
    }
  }
  try {
    await scenario('held-writer', { hold: 500 });
    await scenario('rollback', { fail: true });
    await scenario('outer-owner', { outer: true });
    await scenario('ipc-headroom', { hold: 2500 });
    console.log(JSON.stringify({ scope: 'Four isolated real saveRequestUsage transaction scenarios; not production incident reproduction',
      subjects: reports.length, result: 'pass', reports }));
    console.log('GREEN: actual managed usage contention, duplicate accounting, failure rollback, and transaction ownership');
  } finally {
    await Promise.all([...children].map(async child => {
      const exited = once(child, 'exit');
      child.kill('SIGTERM');
      const force = setTimeout(() => child.kill('SIGKILL'), 2000);
      try { await exited; } finally { clearTimeout(force); }
    }));
    fs.rmSync(root, { recursive: true, force: true });
  }
}
