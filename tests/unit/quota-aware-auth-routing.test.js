import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getProviderConnections: vi.fn(),
  getSettings: vi.fn(),
  updateProviderConnection: vi.fn(),
  resolveConnectionProxyConfig: vi.fn(),
  getClaudeUsage: vi.fn(),
  getCodexUsage: vi.fn(),
}));

vi.mock("@/lib/localDb", () => ({
  getProviderConnections: mocks.getProviderConnections,
  getSettings: mocks.getSettings,
  getProxyPools: vi.fn(),
  validateApiKey: vi.fn(),
  updateProviderConnection: mocks.updateProviderConnection,
}));
vi.mock("@/lib/network/connectionProxy", () => ({
  resolveConnectionProxyConfig: mocks.resolveConnectionProxyConfig,
  pickProxyPoolId: vi.fn(),
}));
vi.mock("@/shared/constants/providers.js", () => ({
  FREE_PROVIDERS: {},
  resolveProviderId: (provider) => provider,
}));
vi.mock("open-sse/services/usage/claude.js", () => ({
  getClaudeUsage: mocks.getClaudeUsage,
}));
vi.mock("open-sse/services/usage/codex.js", () => ({
  getCodexUsage: mocks.getCodexUsage,
}));
vi.mock("@/sse/utils/logger.js", () => ({
  debug: vi.fn(),
  info: vi.fn(),
  warn: vi.fn(),
}));

vi.mock("open-sse/index.js", () => ({}));
vi.mock("@/sse/services/model.js", () => ({ getModelInfo: async () => ({ provider: "claude", model: "claude-sonnet-4-6" }), getComboModels: async () => null }));
vi.mock("open-sse/handlers/chatCore.js", () => ({ handleChatCore: vi.fn(() => { throw new Error("Inference forbidden"); }) }));
vi.mock("@/sse/services/tokenRefresh.js", () => ({ updateProviderCredentials: vi.fn(), checkAndRefreshToken: vi.fn() }));
vi.mock("@/lib/headroom/detect", () => ({ DEFAULT_HEADROOM_URL: "" }));
vi.mock("@/lib/pxpipe/loader.js", () => ({ getTransform: vi.fn() }));
vi.mock("@/lib/pxpipe/events.js", () => ({ appendPxpipeEvent: vi.fn() }));
vi.mock("open-sse/services/combo.js", () => ({ handleComboChat: vi.fn(), handleFusionChat: vi.fn(), detectRequiredCapabilities: () => new Set() }));
vi.mock("open-sse/services/capacityAdapter.js", () => ({ augmentModelsWithCapacityAdapter: models => models, withCapacityAdapterStripping: fn => fn, getActiveAdapterStrategy: vi.fn() }));
vi.mock("open-sse/utils/bypassHandler.js", () => ({ handleBypassRequest: () => null }));
const { handleChat } = await import("@/sse/handlers/chat.js");
const { getProviderCredentials } = await import("@/sse/services/auth.js");

beforeEach(() => {
  vi.clearAllMocks();
  mocks.resolveConnectionProxyConfig.mockResolvedValue({});
  mocks.getSettings.mockResolvedValue({
    quotaAwareSelection: true,
    quotaCacheTtlMs: 45000,
    quotaAwareProviders: ["claude", "codex"],
    fallbackStrategy: "fill-first",
  });
});

describe("polling isolation", () => {
  it("shares account usage across concurrent models without persisting raw token keys", async () => {
    const token = "mock-private-credential";
    mocks.getProviderConnections.mockResolvedValue([{ id: "cross-model", accessToken: token }]);
    mocks.getCodexUsage.mockResolvedValue({ plan: "pro", quotas: { weekly: { remaining: 50 }, spark_weekly: { remaining: 0 } } });
    const keys = [];
    const originalSet = Map.prototype.set;
    const spy = vi.spyOn(Map.prototype, "set").mockImplementation(function(key, value) { keys.push(key); return originalSet.call(this, key, value); });
    try {
      const [normal, spark] = await Promise.all([
        getProviderCredentials("codex", null, "gpt-6-astra"),
        getProviderCredentials("codex", null, "gpt-5.3-codex-spark"),
      ]);
      expect(normal.connectionId).toBe("cross-model");
      expect(spark).toMatchObject({ allRateLimited: true, retryAfter: null });
      expect(mocks.getCodexUsage).toHaveBeenCalledTimes(1);
      expect(keys.some(key => String(key).includes(token))).toBe(false);
      mocks.getProviderConnections.mockResolvedValue([{ id: "cross-model", accessToken: "rotated-mock" }]);
      await getProviderCredentials("codex", null, "gpt-6-astra");
      expect(mocks.getCodexUsage).toHaveBeenCalledTimes(2);
    } finally { spy.mockRestore(); }
  });

  it.each([false, true])("returns chat 429 with unknown resets (mixed known=%s)", async mixed => {
    mocks.getProviderConnections.mockResolvedValue([{ id: `http-unknown-${mixed}`, accessToken: "unknown" }, { id: `http-other-${mixed}`, accessToken: "other" }]);
    mocks.getClaudeUsage.mockImplementation(async token => ({ quotas: { "weekly (7d)": { remaining: 0, resetAt: mixed && token === "other" ? "2099-10-01T03:00:00Z" : null } } }));
    const response = await handleChat(new Request("http://localhost/v1/chat/completions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: "claude/claude-sonnet-4-6", messages: [] }) }));
    expect(response.status).toBe(429);
    expect(response.headers.has("Retry-After")).toBe(mixed);
    const body = await response.json();
    expect(body.error.message).toContain("blocking quota exhausted");
    expect(body.error.message).not.toContain("null");
  });

  it.each(["claude", "codex"])("bounds %s polling and allows unrelated providers to progress", async (provider) => {
    vi.useFakeTimers();
    let signal;
    mocks.getProviderConnections.mockImplementation(async ({ provider: id }) => [{ id: `slow-${provider}-${id}`, accessToken: "offline" }]);
    const fetcher = provider === "claude" ? mocks.getClaudeUsage : mocks.getCodexUsage;
    fetcher.mockImplementation((_token, _proxy, options) => { signal = options.signal; return new Promise(() => {}); });
    try {
      const pending = getProviderCredentials(provider);
      await vi.advanceTimersByTimeAsync(0);
      expect(signal.aborted).toBe(false);
      let unrelated;
      const other = getProviderCredentials("other").then(value => { unrelated = value; });
      await vi.advanceTimersByTimeAsync(0);
      expect(unrelated?.connectionId).toBe(`slow-${provider}-other`);
      await other;
      let completed = false;
      pending.then(() => { completed = true; });
      await vi.advanceTimersByTimeAsync(2000);
      expect(completed).toBe(true);
      await expect(pending).resolves.toMatchObject({ connectionId: `slow-${provider}-${provider}` });
      expect(signal.aborted).toBe(true);
    } finally { vi.useRealTimers(); }
  });
  it("returns the earliest fully-known account reset, not its earliest window", async () => {
    mocks.getProviderConnections.mockResolvedValue([{ id: "reset-a", accessToken: "a" }, { id: "reset-b", accessToken: "b" }]);
    mocks.getClaudeUsage.mockImplementation(async token => ({ quotas: {
      "weekly (7d)": { remaining: 0, resetAt: "2026-10-01T01:00:00Z" },
      "weekly sonnet (7d)": { remaining: 0, resetAt: token === "a" ? null : "2026-10-01T03:00:00Z" },
    } }));
    await expect(getProviderCredentials("claude", null, "claude-sonnet-4-6")).resolves.toMatchObject({ allRateLimited: true, retryAfter: "2026-10-01T03:00:00.000Z" });
  });
  it.each([false, true])("orders fully-known resets chronologically (reverse=%s)", async reverse => {
    const rows = [{ id: `chrono-a-${reverse}`, accessToken: "chrono-a" }, { id: `chrono-b-${reverse}`, accessToken: "chrono-b" }];
    mocks.getProviderConnections.mockResolvedValue(reverse ? rows.reverse() : rows);
    mocks.getClaudeUsage.mockImplementation(async token => ({ quotas: {
      "weekly (7d)": { remaining: 0, resetAt: token === "chrono-a" ? "+010000-01-01T00:00:00.000Z" : "2099-10-01T01:00:00Z" },
    } }));
    await expect(getProviderCredentials("claude", null, "claude-sonnet-4-6")).resolves.toMatchObject({ allRateLimited: true, retryAfter: "2099-10-01T01:00:00.000Z" });
  });
  it("serializes fresh round-robin state after concurrent polling", async () => {
    const rows = [{ id: "race-a", accessToken: "a" }, { id: "race-b", accessToken: "b" }];
    mocks.getSettings.mockResolvedValue({ quotaAwareSelection: true, fallbackStrategy: "round-robin", stickyRoundRobinLimit: 1 });
    mocks.getProviderConnections.mockImplementation(async () => rows.map(row => ({ ...row })));
    mocks.getCodexUsage.mockResolvedValue({ quotas: { session: { remaining: 50 } } });
    mocks.updateProviderConnection.mockImplementation(async (id, patch) => Object.assign(rows.find(row => row.id === id), patch));
    const selected = await Promise.all([getProviderCredentials("codex"), getProviderCredentials("codex")]);
    expect(new Set(selected.map(row => row.connectionId)).size).toBe(2);
    expect(mocks.getCodexUsage).toHaveBeenCalledTimes(2);
  });
});

describe("Claude remaining-first routing", () => {
  it("prefers the account with higher session remaining", async () => {
    mocks.getProviderConnections.mockResolvedValue([
      { id: "cl-low", email: "low@example.com", isActive: true, accessToken: "t-low", priority: 1 },
      { id: "cl-high", email: "high@example.com", isActive: true, accessToken: "t-high", priority: 2 },
    ]);
    mocks.getClaudeUsage.mockImplementation(async (token) => {
      if (token === "t-high") {
        return { quotas: { "session (5h)": { remaining: 80, total: 100, remainingPercentage: 80 }, "weekly (7d)": { remaining: 50, total: 100, remainingPercentage: 50 } } };
      }
      return { quotas: { "session (5h)": { remaining: 10, total: 100, remainingPercentage: 10 }, "weekly (7d)": { remaining: 50, total: 100, remainingPercentage: 50 } } };
    });

    await expect(getProviderCredentials("claude")).resolves.toMatchObject({
      connectionId: "cl-high",
    });
  });

  it("skips account with blocking weekly exhausted", async () => {
    mocks.getProviderConnections.mockResolvedValue([
      { id: "cl-dead", email: "dead@example.com", isActive: true, accessToken: "t-dead" },
      { id: "cl-ok", email: "ok@example.com", isActive: true, accessToken: "t-ok" },
    ]);
    mocks.getClaudeUsage.mockImplementation(async (token) => {
      if (token === "t-dead") {
        return { quotas: { "session (5h)": { remaining: 90, total: 100, remainingPercentage: 90 }, "weekly (7d)": { remaining: 0, total: 100, remainingPercentage: 0 } } };
      }
      return { quotas: { "session (5h)": { remaining: 20, total: 100, remainingPercentage: 20 }, "weekly (7d)": { remaining: 40, total: 100, remainingPercentage: 40 } } };
    });

    await expect(getProviderCredentials("claude")).resolves.toMatchObject({
      connectionId: "cl-ok",
    });
  });

  it("uses DB order when quotaAwareSelection is false", async () => {
    mocks.getSettings.mockResolvedValue({
      quotaAwareSelection: false,
      fallbackStrategy: "fill-first",
    });
    mocks.getProviderConnections.mockResolvedValue([
      { id: "cl-first", email: "first@example.com", isActive: true, accessToken: "t1", priority: 1 },
      { id: "cl-second", email: "second@example.com", isActive: true, accessToken: "t2", priority: 2 },
    ]);

    await expect(getProviderCredentials("claude")).resolves.toMatchObject({
      connectionId: "cl-first",
    });
    expect(mocks.getClaudeUsage).not.toHaveBeenCalled();
  });

  it("returns allRateLimited when every account is blocking-exhausted", async () => {
    const resetAt = "2026-09-03T12:00:00.000Z";
    mocks.getProviderConnections.mockResolvedValue([
      { id: "cl-a", email: "a@example.com", isActive: true, accessToken: "t-a" },
      { id: "cl-b", email: "b@example.com", isActive: true, accessToken: "t-b" },
    ]);
    mocks.getClaudeUsage.mockResolvedValue({
      quotas: {
        "session (5h)": { remaining: 50, total: 100, remainingPercentage: 50 },
        "weekly (7d)": { remaining: 0, total: 100, remainingPercentage: 0, resetAt },
      },
    });

    await expect(getProviderCredentials("claude")).resolves.toMatchObject({
      allRateLimited: true,
      retryAfter: resetAt,
    });
  });
});
