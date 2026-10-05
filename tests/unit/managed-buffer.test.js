import { it, expect, vi } from 'vitest';
import managed from '../../src/lib/db/managed.cjs';
const fixture = vi.hoisted(() => ({ fail: false }));
vi.mock('../../src/lib/db/driver.js', () => ({ getAdapter: async () => ({
  transaction: fn => fn(), run: () => { if (fixture.fail) throw new Error('isolated write failure'); }, get: () => ({ c: 1 }),
}) }));
vi.mock('../../src/lib/db/repos/settingsRepo.js', () => ({ getSettings: async () => ({ enableObservability: true, observabilityFlushIntervalMs: 20 }) }));
it('counts buffered detail work through actual transaction and fails closed on write loss', async () => {
  process.env.NINEROUTER_MANAGED_WORKER = '1';
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
  delete process.env.NINEROUTER_MANAGED_WORKER;
});
