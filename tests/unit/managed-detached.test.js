import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import managed from '../../src/lib/db/managed.cjs';
const jobs = vi.hoisted(() => ({ usage: null, detail: null }));
vi.mock('@/lib/usageDb.js', () => ({
  saveRequestUsage: () => jobs.usage,
  saveRequestDetail: () => jobs.detail,
  appendRequestLog: async () => {}, trackPendingRequest: () => {},
}));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
beforeEach(() => vi.stubEnv('NINEROUTER_MANAGED_WORKER', '1'));
afterEach(() => {
  vi.unstubAllEnvs();
  delete globalThis[Symbol.for('9router.managed.work')];
});
it('retains detached usage persistence after callback returns', async () => {
  const task = deferred(); jobs.usage = task.promise;
  const { saveUsageStats } = await import('../../open-sse/handlers/chatCore/requestDetail.js');
  saveUsageStats({ provider: 'openai', model: 'test', tokens: { prompt_tokens: 1 }, silent: true });
  expect(managed.workState().persistence).toBe(1);
  task.resolve(); await Promise.resolve(); await Promise.resolve();
  expect(managed.workState().persistence).toBe(0);
});
it('retains websocket event work after socket closure and preserves local fetch', async () => {
  const task = deferred();
  const socket = new EventEmitter(); socket.write = vi.fn(); socket.end = vi.fn();
  const fetchLocalResponses = vi.fn(() => task.promise);
  const { createResponsesWsSession } = await import('../../open-sse/handlers/responsesWs/session.js');
  createResponsesWsSession({ socket, req: { headers: {} }, fetchLocalResponses });
  const body = Buffer.from(JSON.stringify({ type: 'response.create', model: 'openai/test' }));
  const mask = Buffer.from([1, 2, 3, 4]);
  const encoded = Buffer.from(body); for (let i = 0; i < encoded.length; i++) encoded[i] ^= mask[i % 4];
  socket.emit('data', Buffer.concat([Buffer.from([0x81, 0x80 | body.length]), mask, encoded]));
  await Promise.resolve();
  expect(managed.workState().websocket).toBe(1);
  socket.emit('close');
  expect(managed.workState().websocket).toBe(1);
  expect(fetchLocalResponses.mock.calls[0][0]).toBe('/v1/responses');
  task.resolve(new Response('data: [DONE]\n\n'));
  await vi.waitFor(() => expect(managed.workState().websocket).toBe(0));
});
