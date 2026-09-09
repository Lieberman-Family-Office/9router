import { describe, expect, it, vi } from "vitest";
const fetch = vi.hoisted(() => vi.fn());
vi.mock("../../open-sse/utils/proxyFetch.js", () => ({ proxyAwareFetch: fetch }));
import { getClaudeUsage } from "../../open-sse/services/usage/claude.js";
import { getCodexUsage } from "../../open-sse/services/usage/codex.js";

describe("offline usage request cancellation", () => {
  it.each([getClaudeUsage, getCodexUsage])("passes cancellation to the actual fetch boundary", async (usage) => {
    const controller = new AbortController();
    fetch.mockImplementation((_url, options) => new Promise((_, reject) => {
      expect(options.signal).toBe(controller.signal);
      options.signal.addEventListener("abort", () => reject(new Error("offline abort")), { once: true });
    }));
    const result = usage("offline", {}, { signal: controller.signal }).catch(error => ({ message: error.message }));
    controller.abort();
    expect((await result).message).toContain("offline abort");
  });
  it.each([undefined, null, {}])("preserves proxy defaults and legacy cancellation (%s)", async proxy => {
    const controller = new AbortController();
    fetch.mockReset();
    fetch.mockResolvedValueOnce({ ok: false, status: 503 });
    fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ organization_id: "offline-org" }) });
    fetch.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    await getClaudeUsage("offline-defaults", proxy, { signal: controller.signal });
    expect(fetch).toHaveBeenCalledTimes(3);
    for (const [, options, actualProxy] of fetch.mock.calls) {
      expect(options.signal).toBe(controller.signal);
      expect(actualProxy).toBe(proxy === undefined ? null : proxy);
    }
  });
  it("retains cancellation across both Claude legacy requests", async () => {
    const controller = new AbortController();
    fetch.mockReset();
    fetch.mockResolvedValueOnce({ ok: false, status: 503 });
    fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ organization_id: "offline-org" }) });
    fetch.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    await getClaudeUsage("offline-legacy", {}, { signal: controller.signal });
    expect(fetch).toHaveBeenCalledTimes(3);
    for (const [, options] of fetch.mock.calls) expect(options.signal).toBe(controller.signal);
  });
});
