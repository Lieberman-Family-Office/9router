import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { once } from 'node:events';
import managed from '../../src/lib/db/managed.cjs';
const settings = vi.hoisted(() => ({ row: { data: '{}' }, error: null }));
vi.mock('../../src/lib/db/driver.js', () => ({ getAdapter: async () => ({ get: () => {
  if (settings.error) throw settings.error;
  return settings.row;
} }) }));
vi.mock('@/lib/localDb', () => ({ getSettings: async () => ({}) }));
vi.mock('@/lib/tunnel', () => ({}));
vi.mock('@/mitm/manager', () => ({ initDbHooks: () => {} }));
vi.mock('@/lib/mitmAliasCache', () => ({}));
vi.mock('@/lib/mcp/stdioSseBridge', () => ({}));
const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), '9r-owner-')));
fs.chmodSync(root, 0o700);
const env = { ...process.env };
const registryKeys = [Symbol.for('9router.managed.work'), Symbol.for('9router.managed.responsesWsReady')];
let registry;
beforeEach(async () => {
  registry = registryKeys.map(key => Object.getOwnPropertyDescriptor(globalThis, key));
  for (const key of registryKeys) delete globalThis[key];
  settings.row = { data: '{}' }; settings.error = null;
  await managed.setResponsesWsReady(Promise.resolve());
});
afterEach(() => {
  process.env = { ...env };
  settings.row = { data: '{}' }; settings.error = null;
  registryKeys.forEach((key, index) => {
    delete globalThis[key];
    if (registry[index]) Object.defineProperty(globalThis, key, registry[index]);
  });
});
describe('managed worker ownership', () => {
  it('refuses app infrastructure before registering cleanup signals', async () => {
    process.env.NINEROUTER_MANAGED_WORKER = '1';
    const before = process.listenerCount('SIGTERM');
    settings.row = { data: JSON.stringify({ tunnelEnabled: true }) };
    const { initializeApp } = await import('../../src/shared/services/initializeApp.js');
    await expect(initializeApp()).rejects.toThrow('app-owned ingress');
    expect(process.listenerCount('SIGTERM')).toBe(before);
  });
  for (const data of ['{bad', 'null', '[]', 'true', '3', '"text"', null, {}]) {
    it(`refuses stored non-object or malformed settings ${JSON.stringify(data)}`, async () => {
      process.env.NINEROUTER_MANAGED_WORKER = '1';
      settings.row = { data };
      const before = process.listenerCount('SIGTERM');
      const { initializeApp } = await import('../../src/shared/services/initializeApp.js');
      await expect(initializeApp()).rejects.toThrow();
      expect(managed.workState().initialized).toBe(false);
      expect(managed.workState().unknown).toBe(true);
      expect(process.listenerCount('SIGTERM')).toBe(before);
    });
  }
  it('refuses unreadable settings rather than defaulting', async () => {
    process.env.NINEROUTER_MANAGED_WORKER = '1';
    settings.error = new Error('settings database unreadable');
    const { initializeApp } = await import('../../src/shared/services/initializeApp.js');
    await expect(initializeApp()).rejects.toThrow('database unreadable');
    expect(managed.workState().initialized).toBe(false);
    expect(managed.workState().unknown).toBe(true);
  });
  it('keeps legacy defaults while strict startup reads only stored objects', async () => {
    const { getSettings, getManagedIngressSettings } = await import('../../src/lib/db/repos/settingsRepo.js');
    settings.row = { data: '{bad' };
    expect((await getSettings()).tunnelEnabled).toBe(false);
    await expect(getManagedIngressSettings()).rejects.toThrow();
    for (const row of [undefined, { data: '{}' }, { data: '{"tunnelEnabled":false}' }]) {
      settings.row = row;
      expect(await getManagedIngressSettings()).toEqual(row ? JSON.parse(row.data) : {});
    }
  });
  it('does not initialize or report private version before delayed attachment settles', async () => {
    process.env.NINEROUTER_MANAGED_WORKER = '1';
    let finish;
    managed.setResponsesWsReady(new Promise((resolve, reject) => { finish = reject; }));
    const { initializeApp } = await import('../../src/shared/services/initializeApp.js');
    const { GET } = await import('../../src/app/api/version/route.js');
    let initialized = false;
    let version = false;
    const init = initializeApp().then(() => { initialized = true; });
    const probe = GET().then(() => { version = true; });
    await Promise.resolve(); await Promise.resolve();
    expect(initialized).toBe(false); expect(version).toBe(false);
    expect(managed.workState().initialized).toBe(false);
    const refused = Promise.all([
      expect(init).rejects.toThrow('attachment failed'),
      expect(probe).rejects.toThrow('attachment failed'),
    ]);
    finish(new Error('pinned attachment failed'));
    await refused;
  });
  for (const missing of [true, false]) {
    it(`refuses ${missing ? 'missing' : 'failed'} attachment readiness`, async () => {
      process.env.NINEROUTER_MANAGED_WORKER = '1';
      if (missing) delete globalThis[registryKeys[1]];
      else managed.setResponsesWsReady(Promise.reject(new Error('pinned attachment failed')));
      const { GET } = await import('../../src/app/api/version/route.js');
      await expect(GET()).rejects.toThrow(missing ? 'attachment missing' : 'attachment failed');
      expect(managed.workState().initialized).toBe(false);
    });
  }
  it('starts both schedulers without inactive ticks, then follows activation/drain', async () => {
    process.env.NINEROUTER_MANAGED_WORKER = '1';
    process.env.NINEROUTER_SLOT = 'a';
    process.env.NINEROUTER_HOTSWAP_RUNTIME = root;
    const server = net.createServer();
    server.listen(path.join(root, 'a.sock')); await once(server, 'listening');
    const { runBackgroundTokenRefreshTick, stopBackgroundTokenRefresh } = await import('../../src/sse/services/backgroundTokenRefresh.js');
    const { runQuotaAutoPingTick, stopQuotaAutoPing } = await import('../../src/shared/services/quotaAutoPing.js');
    try {
      settings.row = { data: '{"tunnelEnabled":false,"tailscaleEnabled":false,"mitmEnabled":false}' };
      const { initializeApp } = await import('../../src/shared/services/initializeApp.js');
      await initializeApp();
      expect(managed.workState().initialized).toBe(true);
      const load = vi.fn(async () => []);
      await runBackgroundTokenRefreshTick({ loadConnections: load });
      expect(load).not.toHaveBeenCalled();
      fs.symlinkSync(path.join(root, 'a.sock'), path.join(root, 'active.sock'));
      let finish;
      const pending = runBackgroundTokenRefreshTick({ loadConnections: () => new Promise(resolve => { finish = resolve; }) });
      expect(managed.workState().background).toBe(1);
      managed.workState().draining = true;
      await runBackgroundTokenRefreshTick({ loadConnections: load });
      expect(load).not.toHaveBeenCalled();
      finish([]); await pending;
      expect(managed.workState().background).toBe(0);
      const getSettings = vi.fn(async () => ({}));
      await runQuotaAutoPingTick({ getSettings }); expect(getSettings).not.toHaveBeenCalled();
      managed.workState().draining = false;
      await runQuotaAutoPingTick({ getSettings }); expect(getSettings).toHaveBeenCalledOnce();
      expect(managed.workState().quota).toBe(0);
    } finally {
      stopBackgroundTokenRefresh(); stopQuotaAutoPing();
      await new Promise(resolve => server.close(resolve));
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});
