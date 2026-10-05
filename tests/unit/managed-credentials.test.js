import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import managed from '../../src/lib/db/managed.cjs';
import { TABLES, buildCreateTableSql } from '../../src/lib/db/schema.js';

let root;
let db;
const saved = { ...process.env };
beforeEach(async () => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), '9r-credentials-'));
  const directory = path.join(root, 'db');
  fs.mkdirSync(directory, { mode: 0o700 });
  const file = path.join(directory, 'data.sqlite');
  fs.writeFileSync(file, '', { mode: 0o600 });
  db = new DatabaseSync(file);
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
  process.env.DATA_DIR = root;
  process.env.NINEROUTER_MANAGED_WORKER = '1';
  process.env.NINEROUTER_HOTSWAP_MANIFEST = receipt;
  process.env.NINEROUTER_HOTSWAP_ENROLLED_MANIFEST = receipt;
  process.env.NINEROUTER_HOTSWAP_REFRESH_DB = refresh;
  delete global._dbAdapter;
  vi.resetModules();
});
afterEach(() => {
  global._dbAdapter?.instance?.close();
  delete global._dbAdapter;
  db?.close();
  fs.rmSync(root, { recursive: true, force: true });
  for (const key of ['DATA_DIR', 'NINEROUTER_MANAGED_WORKER', 'NINEROUTER_HOTSWAP_MANIFEST', 'NINEROUTER_HOTSWAP_ENROLLED_MANIFEST', 'NINEROUTER_HOTSWAP_REFRESH_DB']) {
    if (saved[key] === undefined) delete process.env[key]; else process.env[key] = saved[key];
  }
});

describe('managed generation persistence', () => {
  for (const order of [[1, 2], [2, 1]]) {
    it(`pins generation CAS for callback ordering ${order.join(',')}`, async () => {
      const { updateProviderConnection, getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
      for (const generation of order) {
        await updateProviderConnection('fake-id', { accessToken: `fake-${generation}` }, generation);
        await updateProviderConnection('fake-id', { usageMarker: generation });
      }
      const result = await getProviderConnectionById('fake-id');
      expect(result.accessToken).toBe('fake-2');
      expect(result.refreshGeneration).toBe(2);
      expect(result.usageMarker).toBe(order[1]);
      await expect(updateProviderConnection('fake-id', { accessToken: 'fake-ungated' })).rejects.toThrow('generation CAS');
      await expect(updateProviderConnection('fake-id', { accessToken: 'fake-bool' }, true)).rejects.toThrow('generation');
      expect((await getProviderConnectionById('fake-id')).accessToken).toBe('fake-2');
    });
  }
  it('refuses actual schema mutation before migration and native fallback', async () => {
    db.exec('ALTER TABLE settings ADD COLUMN incompatible TEXT');
    const fallback = vi.fn();
    vi.doMock('@/lib/db/adapters/sqljsAdapter.js', () => ({ createSqlJsAdapter: fallback }));
    const { getAdapter } = await import('@/lib/db/driver.js');
    await expect(getAdapter()).rejects.toThrow('layout mismatch');
    expect(fallback).not.toHaveBeenCalled();
    expect(db.prepare('PRAGMA table_info(settings)').all().map(row => row.name)).toContain('incompatible');
  });
});
