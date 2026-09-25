// Usage-limit errors fail over with a SHORT lock — there is no 12 h lock and no switch for
// one (removed: a fixed 12 h guess ignored provider reset times and a loose "usage limit"
// substring could park a healthy account for half a day). These lock production's measured
// behaviour: 7 s (rate-limit backoff) and 29-30 s (transient lock), always failing over.
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getProviderConnections: vi.fn(),
  getSettings: vi.fn(),
  updateProviderConnection: vi.fn(),
  handleChatCore: vi.fn(),
}));

vi.mock("@/lib/localDb", () => ({
  getProviderConnections: mocks.getProviderConnections,
  getSettings: mocks.getSettings,
  getProxyPools: vi.fn(),
  validateApiKey: vi.fn(),
  updateProviderConnection: mocks.updateProviderConnection,
}));
vi.mock("@/lib/network/connectionProxy", () => ({ resolveConnectionProxyConfig: vi.fn(async () => ({})), pickProxyPoolId: vi.fn() }));
vi.mock("@/shared/constants/providers.js", () => ({ FREE_PROVIDERS: {}, resolveProviderId: (p) => p }));
vi.mock("@/sse/utils/logger.js", () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn() }));
vi.mock("open-sse/index.js", () => ({}));
vi.mock("@/sse/services/model.js", () => ({ getModelInfo: async () => ({ provider: "openai", model: "gpt-x" }), getComboModels: async () => null }));
vi.mock("open-sse/handlers/chatCore.js", () => ({ handleChatCore: mocks.handleChatCore }));
vi.mock("@/sse/services/tokenRefresh.js", () => ({ updateProviderCredentials: vi.fn(), checkAndRefreshToken: vi.fn(async (_p, c) => c) }));
vi.mock("@/lib/headroom/detect", () => ({ DEFAULT_HEADROOM_URL: "" }));
vi.mock("@/lib/pxpipe/loader.js", () => ({ getTransform: vi.fn() }));
vi.mock("@/lib/pxpipe/events.js", () => ({ appendPxpipeEvent: vi.fn() }));
vi.mock("open-sse/services/capacityAdapter.js", () => ({ augmentModelsWithCapacityAdapter: (m) => m, withCapacityAdapterStripping: (fn) => fn, getActiveAdapterStrategy: vi.fn() }));
vi.mock("open-sse/utils/bypassHandler.js", () => ({ handleBypassRequest: () => null }));

const { checkFallbackError } = await import("../../open-sse/services/accountFallback.js");
const { markAccountUnavailable } = await import("@/sse/services/auth.js");
const { handleChat } = await import("@/sse/handlers/chat.js");

const T30 = 30 * 1000; // TRANSIENT_COOLDOWN_MS
const HOUR = 60 * 60 * 1000;
const USER_KEY = '{"error":{"message":"User provided API key rate limit exceeded"}}';
const USAGE = '{"error":{"message":"You have hit your usage limit. Try again later."}}';

beforeEach(() => {
  vi.clearAllMocks();
  mocks.handleChatCore.mockReset();
  mocks.getSettings.mockResolvedValue({ quotaAwareSelection: false, fallbackStrategy: "fill-first" });
  mocks.getProviderConnections.mockResolvedValue([
    { id: "acct-a", provider: "openai", isActive: true, priority: 1, accessToken: "a" },
    { id: "acct-b", provider: "openai", isActive: true, priority: 2, accessToken: "b" },
  ]);
});

describe("usage-limit errors: short lock, fail over", () => {
  it("the retired USAGE_LIMIT_LOCK_12H env var has no effect", () => {
    vi.stubEnv("USAGE_LIMIT_LOCK_12H", "true");
    try {
      expect(checkFallbackError(400, USAGE)).toEqual({ shouldFallback: true, cooldownMs: T30 });
      expect(checkFallbackError(429, USER_KEY, 0).cooldownMs).toBeLessThan(HOUR);
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("'user provided api key rate limit exceeded' gets the generic rate-limit backoff", () => {
    const expected = { shouldFallback: true, cooldownMs: 2000, newBackoffLevel: 1 };
    expect(checkFallbackError(429, USER_KEY, 0)).toEqual(expected);
    expect(checkFallbackError(400, USER_KEY, 0)).toEqual(expected);
  });

  it("a 400/422 'usage limit' gets the 30 s transient lock and fails over, not terminal-400", () => {
    expect(checkFallbackError(400, USAGE)).toEqual({ shouldFallback: true, cooldownMs: T30 });
    expect(checkFallbackError(422, USAGE)).toEqual({ shouldFallback: true, cooldownMs: T30 });
  });

  it("a 429 'usage limit' keeps the 429 backoff", () => {
    expect(checkFallbackError(429, USAGE, 0)).toEqual({ shouldFallback: true, cooldownMs: 2000, newBackoffLevel: 1 });
  });

  it("a plain 400 without either text is still terminal", () => {
    expect(checkFallbackError(400, '{"error":{"message":"bad tool_choice"}}')).toEqual({ shouldFallback: false, cooldownMs: 0 });
  });

  it("markAccountUnavailable writes a 30 s lock", async () => {
    const t0 = Date.now();
    await markAccountUnavailable("acct-a", 400, USAGE, "openai", "gpt-x");
    const until = new Date(mocks.updateProviderConnection.mock.calls[0][1]["modelLock_gpt-x"]).getTime();
    expect(until - t0).toBeGreaterThanOrEqual(T30 - 1000);
    expect(until - t0).toBeLessThanOrEqual(T30 + 5000);
  });

  it("chat fails over from account A to B on a 400 'usage limit'", async () => {
    mocks.handleChatCore
      .mockResolvedValueOnce({ success: false, status: 400, error: USAGE, response: new Response(USAGE, { status: 400 }) })
      .mockResolvedValueOnce({ success: true, response: new Response('{"ok":true}', { status: 200 }) });
    const res = await handleChat(new Request("http://localhost/v1/chat/completions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: "openai/gpt-x", messages: [] }),
    }));
    expect(res.status).toBe(200);
    expect(mocks.handleChatCore).toHaveBeenCalledTimes(2);
    expect(mocks.updateProviderConnection.mock.calls[0][0]).toBe("acct-a");
  });
});
