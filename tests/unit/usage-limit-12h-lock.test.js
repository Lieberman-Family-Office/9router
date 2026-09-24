// The two 12 h rules production carried only as a bundle patch
// (~/.9router/apply-usage-limit-lock-patch.sh). They must lock for 12 h, fail over,
// and win over both the generic "rate limit" text rule and the terminal 400 status rule.
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

const H12 = 12 * 60 * 60 * 1000; // production patch: cooldownMs:432e5
const USER_KEY = '{"error":{"message":"User provided API key rate limit exceeded"}}';
const USAGE = '{"error":{"message":"You have hit your usage limit. Try again later."}}';

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getSettings.mockResolvedValue({ quotaAwareSelection: false, fallbackStrategy: "fill-first" });
  mocks.getProviderConnections.mockResolvedValue([
    { id: "acct-a", provider: "openai", isActive: true, priority: 1, accessToken: "a" },
    { id: "acct-b", provider: "openai", isActive: true, priority: 2, accessToken: "b" },
  ]);
});

describe("12 h usage-limit rules", () => {
  it("'user provided api key rate limit exceeded' locks 12 h, not generic rate-limit backoff", () => {
    expect(checkFallbackError(429, USER_KEY, 0)).toEqual({ shouldFallback: true, cooldownMs: H12 });
  });

  it("'usage limit' locks 12 h", () => {
    expect(checkFallbackError(429, USAGE, 0)).toEqual({ shouldFallback: true, cooldownMs: H12 });
  });

  it("text wins over terminal status: a 400 carrying either text fails over with 12 h", () => {
    expect(checkFallbackError(400, USER_KEY)).toEqual({ shouldFallback: true, cooldownMs: H12 });
    expect(checkFallbackError(400, USAGE)).toEqual({ shouldFallback: true, cooldownMs: H12 });
    expect(checkFallbackError(422, USAGE)).toEqual({ shouldFallback: true, cooldownMs: H12 });
  });

  it("markAccountUnavailable writes a 12 h model lock", async () => {
    const t0 = Date.now();
    const r = await markAccountUnavailable("acct-a", 400, USAGE, "openai", "gpt-x");
    expect(r).toEqual({ shouldFallback: true, cooldownMs: H12 });
    const [, update] = mocks.updateProviderConnection.mock.calls[0];
    const until = new Date(update["modelLock_gpt-x"]).getTime();
    expect(until - t0).toBeGreaterThanOrEqual(H12 - 1000);
    expect(until - t0).toBeLessThanOrEqual(H12 + 5000);
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
