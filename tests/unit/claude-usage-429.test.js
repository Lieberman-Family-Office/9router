import { afterEach, describe, expect, it, vi } from "vitest";
const fetch = vi.hoisted(() => vi.fn());
vi.mock("../../open-sse/utils/proxyFetch.js", () => ({ proxyAwareFetch: fetch }));
import { getClaudeUsage } from "../../open-sse/services/usage/claude.js";

const ok = { ok: true, status: 200, json: async () => ({ five_hour: { utilization: 40, resets_at: null } }) };
const r429 = (retryAfter = null) => ({ ok: false, status: 429, headers: { get: () => retryAfter } });

afterEach(() => { fetch.mockReset(); vi.useRealTimers(); });

describe("Claude usage on OAuth 429", () => {
  it("reports the rate limit and never calls the legacy admin endpoint", async () => {
    fetch.mockResolvedValueOnce(r429());
    const res = await getClaudeUsage("t-429", null, { force: true });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(res.rateLimited).toBe(true);
    expect(res.message).toMatch(/Rate limited/);
    expect(res.message).not.toMatch(/admin|401/);
    // Cooldown: next call makes no network request at all.
    const again = await getClaudeUsage("t-429", null, { force: true });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(again.rateLimited).toBe(true);
  });

  it("serves the last good reading flagged stale instead of the error", async () => {
    fetch.mockResolvedValueOnce(ok).mockResolvedValueOnce(r429());
    const good = await getClaudeUsage("t-stale", null, { force: true });
    expect(good.quotas["session (5h)"].used).toBe(40);
    const res = await getClaudeUsage("t-stale", null, { force: true });
    expect(res.stale).toBe(true);
    expect(res.fetchedAt).toBe(good.fetchedAt);
    expect(res.quotas["session (5h)"].used).toBe(40);
    expect(res.staleReason).toMatch(/Rate limited/);
  });

  it("bounded callers share a fresh reading but never a stale one", async () => {
    const signal = new AbortController().signal;
    fetch.mockResolvedValueOnce(ok);
    const a = await getClaudeUsage("t-bounded", null, { signal });
    const b = await getClaudeUsage("t-bounded", null, { signal });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(b).toBe(a);
    // Dashboard poll that 429s leaves a stale entry; routing must get the rate-limit, not old quotas.
    fetch.mockResolvedValueOnce(r429());
    expect((await getClaudeUsage("t-bounded", null, { force: true })).stale).toBe(true);
    const c = await getClaudeUsage("t-bounded", null, { signal });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(c.rateLimited).toBe(true);
    expect(c.quotas).toBeUndefined();
  });

  it("backs off exponentially and honours a longer Retry-After", async () => {
    vi.useFakeTimers({ now: 0 });
    fetch.mockResolvedValueOnce(r429()).mockResolvedValueOnce(r429()).mockResolvedValueOnce(r429("7200"));
    const a = await getClaudeUsage("t-backoff", null, { force: true });
    expect(Date.parse(a.retryAt)).toBe(180_000);
    vi.setSystemTime(180_001);
    const b = await getClaudeUsage("t-backoff", null, { force: true });
    expect(Date.parse(b.retryAt)).toBe(180_001 + 360_000);
    vi.setSystemTime(600_000);
    const c = await getClaudeUsage("t-backoff", null, { force: true });
    expect(Date.parse(c.retryAt)).toBe(600_000 + 7_200_000);
  });
});
