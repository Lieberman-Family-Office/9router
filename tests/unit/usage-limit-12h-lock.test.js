// The two 12 h usage-limit rules are opt-in (USAGE_LIMIT_LOCK_12H=true).
// Off (default) must reproduce production's measured behaviour: the bundle patch
// never reached the request path, so the faithful sandbox (run2) showed 7 s / 29 s.
// On, both texts lock 12 h, fail over, and beat the generic "rate limit" rule and
// the terminal 400/422 status rules.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

const H12 = 12 * 60 * 60 * 1000; // production patch: cooldownMs:432e5
const T30 = 30 * 1000; // TRANSIENT_COOLDOWN_MS
const USER_KEY = '{"error":{"message":"User provided API key rate limit exceeded"}}';
const USAGE = '{"error":{"message":"You have hit your usage limit. Try again later."}}';

beforeEach(() => {
  vi.clearAllMocks();
  // clearAllMocks keeps queued mockResolvedValueOnce values; a test that stops early
  // would leak its unused second reply into the next test.
  mocks.handleChatCore.mockReset();
  mocks.getSettings.mockResolvedValue({ quotaAwareSelection: false, fallbackStrategy: "fill-first" });
  mocks.getProviderConnections.mockResolvedValue([
    { id: "acct-a", provider: "openai", isActive: true, priority: 1, accessToken: "a" },
    { id: "acct-b", provider: "openai", isActive: true, priority: 2, accessToken: "b" },
  ]);
});

afterEach(() => vi.unstubAllEnvs());

async function chatFailsOverOn(status, body) {
  mocks.handleChatCore
    .mockResolvedValueOnce({ success: false, status, error: body, response: new Response(body, { status }) })
    .mockResolvedValueOnce({ success: true, response: new Response('{"ok":true}', { status: 200 }) });
  const res = await handleChat(new Request("http://localhost/v1/chat/completions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: "openai/gpt-x", messages: [] }),
  }));
  expect(res.status).toBe(200);
  expect(mocks.handleChatCore).toHaveBeenCalledTimes(2);
  expect(mocks.updateProviderConnection.mock.calls[0][0]).toBe("acct-a");
}

describe("12 h usage-limit rules OFF (default): production's measured behaviour", () => {
  it("unset and any value other than 'true' are off", () => {
    for (const v of [undefined, "", "false", "1", "TRUE", "yes"]) {
      if (v === undefined) vi.unstubAllEnvs(); else vi.stubEnv("USAGE_LIMIT_LOCK_12H", v);
      expect(checkFallbackError(429, USAGE, 0).cooldownMs).not.toBe(H12);
    }
  });

  it("'user provided api key rate limit exceeded' gets the generic rate-limit backoff (run2 replica: 7 s)", () => {
    const expected = { shouldFallback: true, cooldownMs: 2000, newBackoffLevel: 1 };
    expect(checkFallbackError(429, USER_KEY, 0)).toEqual(expected);
    expect(checkFallbackError(400, USER_KEY, 0)).toEqual(expected);
  });

  it("a 400/422 'usage limit' gets the 30 s transient lock and fails over (run2 replica: 29 s), not terminal-400", () => {
    expect(checkFallbackError(400, USAGE)).toEqual({ shouldFallback: true, cooldownMs: T30 });
    expect(checkFallbackError(422, USAGE)).toEqual({ shouldFallback: true, cooldownMs: T30 });
  });

  it("a 429 'usage limit' keeps the 429 backoff, as in production", () => {
    expect(checkFallbackError(429, USAGE, 0)).toEqual({ shouldFallback: true, cooldownMs: 2000, newBackoffLevel: 1 });
  });

  it("a plain 400 without either text is still terminal", () => {
    expect(checkFallbackError(400, '{"error":{"message":"bad tool_choice"}}')).toEqual({ shouldFallback: false, cooldownMs: 0 });
  });

  it("markAccountUnavailable writes a 30 s lock, not 12 h", async () => {
    const t0 = Date.now();
    await markAccountUnavailable("acct-a", 400, USAGE, "openai", "gpt-x");
    const until = new Date(mocks.updateProviderConnection.mock.calls[0][1]["modelLock_gpt-x"]).getTime();
    expect(until - t0).toBeGreaterThanOrEqual(T30 - 1000);
    expect(until - t0).toBeLessThanOrEqual(T30 + 5000);
  });

  it("chat fails over from account A to B on a 400 'usage limit'", async () => {
    await chatFailsOverOn(400, USAGE);
  });
});

describe("12 h usage-limit rules ON (USAGE_LIMIT_LOCK_12H=true)", () => {
  beforeEach(() => vi.stubEnv("USAGE_LIMIT_LOCK_12H", "true"));

  it("'user provided api key rate limit exceeded' locks 12 h, not generic rate-limit backoff", () => {
    expect(checkFallbackError(429, USER_KEY, 0)).toEqual({ shouldFallback: true, cooldownMs: H12 });
  });

  it("'usage limit' locks 12 h", () => {
    expect(checkFallbackError(429, USAGE, 0)).toEqual({ shouldFallback: true, cooldownMs: H12 });
  });

  it("text wins over terminal status: a 400/422 carrying either text fails over with 12 h", () => {
    expect(checkFallbackError(400, USER_KEY)).toEqual({ shouldFallback: true, cooldownMs: H12 });
    expect(checkFallbackError(400, USAGE)).toEqual({ shouldFallback: true, cooldownMs: H12 });
    expect(checkFallbackError(422, USAGE)).toEqual({ shouldFallback: true, cooldownMs: H12 });
  });

  it("markAccountUnavailable writes a 12 h model lock", async () => {
    const t0 = Date.now();
    const r = await markAccountUnavailable("acct-a", 400, USAGE, "openai", "gpt-x");
    expect(r).toEqual({ shouldFallback: true, cooldownMs: H12 });
    const until = new Date(mocks.updateProviderConnection.mock.calls[0][1]["modelLock_gpt-x"]).getTime();
    expect(until - t0).toBeGreaterThanOrEqual(H12 - 1000);
    expect(until - t0).toBeLessThanOrEqual(H12 + 5000);
  });

  it("chat fails over from account A to B on a 400 'usage limit'", async () => {
    await chatFailsOverOn(400, USAGE);
  });
});
