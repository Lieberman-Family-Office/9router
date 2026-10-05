import { describe, it, expect, vi, afterEach } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { once } from 'node:events';
import managed from '../../src/lib/db/managed.cjs';
const settings = vi.hoisted(() => ({ value: {} }));
vi.mock('@/lib/localDb', () => ({ getSettings: async () => settings.value }));
vi.mock('@/lib/tunnel', () => ({}));
vi.mock('@/mitm/manager', () => ({ initDbHooks: () => {} }));
vi.mock('@/lib/mitmAliasCache', () => ({}));
vi.mock('@/lib/mcp/stdioSseBridge', () => ({}));
const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), '9r-owner-')));
fs.chmodSync(root, 0o700);
const env = { ...process.env };
afterEach(() => { process.env = { ...env }; settings.value = {}; managed.workState().draining = false; });
describe('managed worker ownership', () => {
  it('refuses app infrastructure before registering cleanup signals', async () => {
    process.env.NINEROUTER_MANAGED_WORKER = '1';
    const before = process.listenerCount('SIGTERM');
    settings.value = { tunnelEnabled: true };
    const { initializeApp } = await import('../../src/shared/services/initializeApp.js');
    await expect(initializeApp()).rejects.toThrow('app-owned ingress');
    expect(process.listenerCount('SIGTERM')).toBe(before);
  });
  it('starts both schedulers without inactive ticks, then follows activation/drain', async () => {
    process.env.NINEROUTER_MANAGED_WORKER = '1';
    process.env.NINEROUTER_SLOT = 'a';
    process.env.NINEROUTER_HOTSWAP_RUNTIME = root;
    const server = net.createServer();
    server.listen(path.join(root, 'a.sock')); await once(server, 'listening');
    const { runBackgroundTokenRefreshTick, stopBackgroundTokenRefresh } = await import('../../src/sse/services/backgroundTokenRefresh.js');
    const { runQuotaAutoPingTick, stopQuotaAutoPing } = await import('../../src/shared/services/quotaAutoPing.js');
    try {
      managed.workState().unknown = false;
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
