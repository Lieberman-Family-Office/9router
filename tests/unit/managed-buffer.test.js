import { it, expect, vi, beforeEach, afterEach } from 'vitest';
import managed from '../../src/lib/db/managed.cjs';
const fixture = vi.hoisted(() => ({ fail: false, settingsError: null, enabled: true, writes: 0 }));
vi.mock('../../src/lib/db/driver.js', () => ({ getAdapter: async () => ({
  transaction: fn => fn(), run: () => { fixture.writes++; if (fixture.fail) throw new Error('isolated write failure'); }, get: () => ({ c: 1 }),
}) }));
vi.mock('../../src/lib/db/repos/settingsRepo.js', () => ({ getSettings: async () => {
  if (fixture.settingsError) throw fixture.settingsError;
  return { enableObservability: fixture.enabled, observabilityFlushIntervalMs: 20 };
} }));
const registryKey = Symbol.for('9router.managed.work');
let registry;
let env;
beforeEach(() => {
  registry = Object.getOwnPropertyDescriptor(globalThis, registryKey);
  delete globalThis[registryKey];
  env = { ...process.env };
  process.env.NINEROUTER_MANAGED_WORKER = '1';
  delete process.env.ENABLE_REQUEST_LOGS;
  delete process.env.REQUEST_DETAILS_MODE;
  fixture.fail = false; fixture.settingsError = null; fixture.enabled = true; fixture.writes = 0;
  vi.resetModules();
});
afterEach(() => {
  process.env = env;
  delete globalThis[registryKey];
  if (registry) Object.defineProperty(globalThis, registryKey, registry);
});
it('counts buffered detail work through actual transaction and fails closed on write loss', async () => {
  const { saveRequestDetail } = await import('../../src/lib/db/repos/requestDetailsRepo.js');
  const state = managed.workState();
  const record = { id: 'isolated', model: 'test' };
  await saveRequestDetail(record);
  await saveRequestDetail(record);
  expect(state.persistence).toBe(2);
  await vi.waitFor(() => expect(state.persistence).toBe(0));
  fixture.fail = true;
  await saveRequestDetail({ id: 'failed' });
  await vi.waitFor(() => expect(state.unknown).toBe(true));
  expect(state.persistence).toBeGreaterThan(0);
});
it('refuses clean retirement when observability settings reject', async () => {
  fixture.settingsError = new Error('isolated settings failure');
  const { saveRequestDetail } = await import('../../src/lib/db/repos/requestDetailsRepo.js');
  const state = managed.workState();
  state.initialized = true;
  await expect(saveRequestDetail({ id: 'settings-failed' })).rejects.toThrow('isolated settings failure');
  expect(state.persistence).toBe(0);
  expect(state.unknown).toBe(true);
  expect(fixture.writes).toBe(0);
});
it('permits explicitly disabled observability without unknown work', async () => {
  fixture.enabled = false;
  const { saveRequestDetail } = await import('../../src/lib/db/repos/requestDetailsRepo.js');
  await saveRequestDetail({ id: 'disabled' });
  expect(managed.workState().persistence).toBe(0);
  expect(managed.workState().unknown).toBe(false);
  expect(fixture.writes).toBe(0);
});
it('preserves unmanaged disabled fallback on settings rejection', async () => {
  delete process.env.NINEROUTER_MANAGED_WORKER;
  fixture.settingsError = new Error('isolated settings failure');
  const { saveRequestDetail } = await import('../../src/lib/db/repos/requestDetailsRepo.js');
  await saveRequestDetail({ id: 'unmanaged' });
  expect(managed.workState().unknown).toBe(false);
  expect(fixture.writes).toBe(0);
});
