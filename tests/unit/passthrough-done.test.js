import { describe, expect, it, vi } from 'vitest';
vi.mock('@/lib/usageDb.js', () => ({ trackPendingRequest: vi.fn(), appendRequestLog: vi.fn(async () => {}) }));
vi.mock('../../open-sse/utils/usageTracking.js', () => ({ extractUsage: () => null, mergeUsage: () => null, hasValidUsage: () => false, estimateUsage: () => null, logUsage: vi.fn(), addBufferToUsage: value => value, filterUsageForFormat: value => value, COLORS: {} }));
import { createPassthroughStreamWithLogger } from '../../open-sse/utils/stream.js';

async function stream(chunks) {
  const input = new ReadableStream({ start(controller) { for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk)); controller.close(); } });
  return new Response(input.pipeThrough(createPassthroughStreamWithLogger('fixture'))).text();
}

describe('native Responses passthrough terminal framing', () => {
  for (const tail of ['data: [DONE]\n\n', 'data:[DONE]\n\n', 'data: [DONE]']) {
    it(`preserves one upstream terminal sentinel for ${JSON.stringify(tail)}`, async () => {
      const completed = 'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n';
      const output = await stream([completed, tail.slice(0, 5), tail.slice(5)]);
      expect(output.match(/data: \[DONE\]/g)).toHaveLength(1);
      expect(output).toContain('"type":"response.completed"');
    });
  }
  it('adds one missing terminal sentinel', async () => {
    const output = await stream(['data: {"type":"response.completed","response":{"status":"completed"}}\n\n']);
    expect(output.match(/data: \[DONE\]/g)).toHaveLength(1);
  });
});
