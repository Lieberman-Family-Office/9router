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
  db.exec("INSERT INTO providerConnections VALUES('fake-id','github','oauth',NULL,NULL,1,1,'{}','now','now')");
  const manifest = await managed.createManifest(path.resolve(import.meta.dirname, '../..'));
  const receipt = path.join(directory, 'enrolled.json');
  fs.writeFileSync(receipt, JSON.stringify(manifest), { mode: 0o600 });
  const refresh = path.join(directory, 'refresh.sqlite');
  managed.enrollRefreshStore(refresh, 4);
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
  vi.unstubAllGlobals();
});

const oauth = generation => ({ accessToken: `fake-oauth-${generation}`, refreshToken: `fake-rotate-${generation}`,
  expiresAt: `2030-01-0${generation}T00:00:00.000Z`, refreshGenerations: { oauth: generation } });
const copilot = generation => ({ copilotToken: `fake-copilot-${generation}`, copilotTokenExpiresAt: 1900000000 + generation,
  refreshGenerations: { copilot: generation } });

// Enter the persistence dispatch, which calls the real SQLite repository.
describe('managed family generation persistence', () => {
  for (const family of ['oauth', 'copilot']) {
    for (const order of [[1, 2], [2, 1]]) {
      it(`${family} keeps newest credentials through dispatch in order ${order}`, async () => {
        const { updateProviderCredentials } = await import('@/sse/services/tokenRefresh.js');
        const { getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
        for (const generation of order) await updateProviderCredentials('fake-id', (family === 'oauth' ? oauth : copilot)(generation));
        const row = await getProviderConnectionById('fake-id');
        expect(row.refreshGenerations[family]).toBe(2);
        if (family === 'oauth') {
          expect(row.accessToken === oauth(2).accessToken).toBe(true);
          expect(row.refreshToken === oauth(2).refreshToken).toBe(true);
          expect(row.expiresAt).toBe(oauth(2).expiresAt);
        } else {
          expect(row.providerSpecificData.copilotToken === copilot(2).copilotToken).toBe(true);
          expect(row.providerSpecificData.copilotTokenExpiresAt).toBe(copilot(2).copilotTokenExpiresAt);
          expect(row.expiresAt).toBeUndefined();
        }
      });
    }
  }
  for (const order of [['oauth', 'copilot'], ['copilot', 'oauth']]) {
    it(`merges crossed families and stale nested snapshots in order ${order}`, async () => {
      const { updateProviderCredentials } = await import('@/sse/services/tokenRefresh.js');
      const { updateProviderConnection, getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
      await updateProviderConnection('fake-id', { providerSpecificData: { usage: 7, project: 'kept' } });
      for (const family of order) {
        await updateProviderCredentials('fake-id', {
          ...(family === 'oauth' ? oauth(1) : copilot(2)),
          existingProviderSpecificData: { usage: 0, copilotToken: 'fake-stale' },
        });
      }
      const row = await getProviderConnectionById('fake-id');
      expect(row.refreshGenerations).toEqual({ oauth: 1, copilot: 2 });
      expect(row.accessToken === oauth(1).accessToken).toBe(true);
      expect(row.providerSpecificData.copilotToken === copilot(2).copilotToken).toBe(true);
      expect(row.providerSpecificData.usage).toBe(7);
      expect(row.providerSpecificData.project).toBe('kept');
      expect(row.expiresAt).toBe(oauth(1).expiresAt);
    });
  }
  it('merges a combined result independently when only one family is stale', async () => {
    const { updateProviderCredentials } = await import('@/sse/services/tokenRefresh.js');
    const { getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
    await updateProviderCredentials('fake-id', oauth(3));
    await updateProviderCredentials('fake-id', { ...oauth(1), ...copilot(2), refreshGenerations: { oauth: 1, copilot: 2 } });
    const row = await getProviderConnectionById('fake-id');
    expect(row.accessToken === oauth(3).accessToken).toBe(true);
    expect(row.providerSpecificData.copilotToken === copilot(2).copilotToken).toBe(true);
    expect(row.refreshGenerations).toEqual({ oauth: 3, copilot: 2 });
    expect(row.expiresAt).toBe(oauth(3).expiresAt);
    await updateProviderCredentials('fake-id', { ...oauth(4), ...copilot(1), refreshGenerations: { oauth: 4, copilot: 1 } });
    const next = await getProviderConnectionById('fake-id');
    expect(next.accessToken === oauth(4).accessToken).toBe(true);
    expect(next.providerSpecificData.copilotToken === copilot(2).copilotToken).toBe(true);
  });
  it('refuses missing, unknown, and malformed family generations at the actual repository', async () => {
    const { updateProviderConnection } = await import('@/lib/db/repos/connectionsRepo.js');
    for (const patch of [{ accessToken: 'fake' }, { expiresAt: '2030-01-01' }, { apiKey: 'fake' },
      { providerSpecificData: { copilotTokenExpiresAt: 1 } }]) {
      await expect(updateProviderConnection('fake-id', patch)).rejects.toThrow('generation');
    }
    for (const generations of [null, true, 1, {}, { other: 1 }, { oauth: true }, { oauth: NaN },
      { oauth: Infinity }, { oauth: -1 }, { oauth: 0 }, { oauth: 1.5 }, { copilot: 1 }]) {
      await expect(updateProviderConnection('fake-id', { accessToken: 'fake' }, generations)).rejects.toThrow('generation');
    }
  });
  it('carries both generations through the combined refresh dispatch', async () => {
    const issuer = vi.fn(async url => ({ ok: true, json: async () => String(url).includes('copilot')
      ? { token: 'fake-copilot', expires_at: 1900000000 }
      : { access_token: 'fake-oauth', refresh_token: 'fake-rotated', expires_in: 3600 } }));
    vi.stubGlobal('fetch', issuer);
    const { refreshGitHubAndCopilotTokens, updateProviderCredentials } = await import('@/sse/services/tokenRefresh.js');
    const result = await refreshGitHubAndCopilotTokens({ refreshToken: 'fake-old' });
    expect(result.refreshGenerations).toEqual({ oauth: 5, copilot: 6 });
    await updateProviderCredentials('fake-id', result);
    expect(issuer).toHaveBeenCalledTimes(2);
  });
  it('re-reads persisted credentials through proactive dispatch before further refreshes', async () => {
    const { updateProviderConnection, getProviderConnectionById } = await import('@/lib/db/repos/connectionsRepo.js');
    await updateProviderConnection('fake-id', { accessToken: 'fake-current', refreshToken: 'fake-current-refresh',
      expiresAt: '2030-01-01T00:00:00.000Z', providerSpecificData: { copilotToken: 'fake-expired', copilotTokenExpiresAt: 1 } },
      { oauth: 1, copilot: 2 });
    const issuer = vi.fn(async () => ({ ok: true, json: async () => ({ token: 'fake-new-copilot', expires_at: 1900000000 }) }));
    vi.stubGlobal('fetch', issuer);
    const { checkAndRefreshToken } = await import('@/sse/services/tokenRefresh.js');
    const result = await checkAndRefreshToken('github', { connectionId: 'fake-id', accessToken: 'fake-stale', refreshToken: 'fake-stale-refresh' });
    expect(issuer).toHaveBeenCalledTimes(1);
    expect(issuer.mock.calls[0][1].headers.Authorization === 'token fake-current').toBe(true);
    expect(result.accessToken === 'fake-current').toBe(true);
    const row = await getProviderConnectionById('fake-id');
    expect(row.refreshGenerations).toEqual({ oauth: 1, copilot: 5 });
    expect(row.expiresAt).toBe('2030-01-01T00:00:00.000Z');
  });
  it('preserves family ownership through GitHub executor dispatch', async () => {
    const { GithubExecutor } = await import('../../open-sse/executors/github.js');
    const executor = new GithubExecutor();
    executor.refreshCopilotToken = vi.fn().mockResolvedValue({ token: 'fake-copilot', expiresAt: 1900000000, refreshGenerations: { copilot: 2 } });
    const onlyCopilot = await executor.refreshCredentials({ accessToken: 'fake-current', refreshToken: 'fake-old' });
    expect(onlyCopilot.accessToken).toBeUndefined();
    expect(onlyCopilot.refreshToken).toBeUndefined();
    expect(onlyCopilot.refreshGenerations).toEqual({ copilot: 2 });
    executor.refreshCopilotToken = vi.fn().mockResolvedValueOnce(null)
      .mockResolvedValueOnce({ token: 'fake-copilot', expiresAt: 1900000000, refreshGenerations: { copilot: 2 } });
    executor.refreshGitHubToken = vi.fn().mockResolvedValue(oauth(1));
    const combined = await executor.refreshCredentials({ accessToken: 'fake-current', refreshToken: 'fake-old' });
    expect(combined.refreshGenerations).toEqual({ oauth: 1, copilot: 2 });
    const { updateProviderCredentials } = await import('@/sse/services/tokenRefresh.js');
    const { getRefreshWorkStatus } = await import('../../open-sse/services/tokenRefresh/dedup.js');
    const persistence = updateProviderCredentials('fake-id', combined);
    expect(getRefreshWorkStatus().activeRefreshOperations).toBeGreaterThan(0);
    await persistence;
    expect(getRefreshWorkStatus().activeRefreshOperations).toBe(0);
  });
  it('reads native stored settings strictly without changing legacy defaults', async () => {
    const { getManagedIngressSettings, getSettings } = await import('@/lib/db/repos/settingsRepo.js');
    expect(await getManagedIngressSettings()).toEqual({});
    for (const data of ['{bad', 'null', '[]', 'true', '3', '"text"']) {
      db.prepare('INSERT INTO settings(id,data) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data').run(data);
      await expect(getManagedIngressSettings()).rejects.toThrow();
      expect((await getSettings()).tunnelEnabled).toBe(false);
    }
    db.prepare('UPDATE settings SET data=? WHERE id=1').run('{"tunnelEnabled":false,"tailscaleEnabled":false,"mitmEnabled":false}');
    expect(await getManagedIngressSettings()).toEqual({ tunnelEnabled: false, tailscaleEnabled: false, mitmEnabled: false });
    db.exec('DROP TABLE settings');
    await expect(getManagedIngressSettings()).rejects.toThrow();
  });
  it('refuses unsupported native runtime before fallback or migration', async () => {
    const fallback = vi.fn();
    const migration = vi.fn();
    vi.doMock('@/lib/db/adapters/sqljsAdapter.js', () => ({ createSqlJsAdapter: fallback }));
    vi.doMock('@/lib/db/migrate.js', () => ({ runMigrationOnce: migration }));
    Object.defineProperty(process.versions, 'bun', { value: 'test-only', configurable: true });
    try {
      const { getAdapter } = await import('@/lib/db/driver.js');
      await expect(getAdapter()).rejects.toThrow('requires node:sqlite');
      expect(fallback).not.toHaveBeenCalled();
      expect(migration).not.toHaveBeenCalled();
    } finally { delete process.versions.bun; }
  });
  it('refuses actual schema mutation before migration and native fallback', async () => {
    db.exec('ALTER TABLE settings ADD COLUMN incompatible TEXT');
    const fallback = vi.fn();
    vi.doMock('@/lib/db/adapters/sqljsAdapter.js', () => ({ createSqlJsAdapter: fallback }));
    const { getAdapter } = await import('@/lib/db/driver.js');
    await expect(getAdapter()).rejects.toThrow('layout mismatch');
    expect(fallback).not.toHaveBeenCalled();
  });
});
