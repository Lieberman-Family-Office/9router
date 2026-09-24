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
const { handleComboChat } = await import("../../open-sse/services/combo.js");
const { markAccountUnavailable } = await import("@/sse/services/auth.js");
const { handleChat } = await import("@/sse/handlers/chat.js");

const INVALID = '{"type":"error","error":{"type":"invalid_request_error","message":"messages: field required"}}';

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getSettings.mockResolvedValue({ quotaAwareSelection: false, fallbackStrategy: "fill-first" });
  mocks.getProviderConnections.mockResolvedValue([
    { id: "acct-a", provider: "openai", isActive: true, priority: 1, accessToken: "a" },
    { id: "acct-b", provider: "openai", isActive: true, priority: 2, accessToken: "b" },
  ]);
});

describe("checkFallbackError classification", () => {
  it("400 invalid_request_error is terminal: no fallback, no cooldown", () => {
    expect(checkFallbackError(400, INVALID)).toEqual({ shouldFallback: false, cooldownMs: 0 });
  });

  it("422 is terminal: no fallback, no cooldown", () => {
    expect(checkFallbackError(422, "Unprocessable Entity")).toEqual({ shouldFallback: false, cooldownMs: 0 });
  });

  it("account-level 400s still fail over", () => {
    expect(checkFallbackError(400, "Your credit balance is too low to access the Anthropic API."))
      .toEqual({ shouldFallback: true, cooldownMs: 120000 });
    expect(checkFallbackError(400, "API key not valid. Please pass a valid API key."))
      .toEqual({ shouldFallback: true, cooldownMs: 120000 });
  });

  it("existing classifications are unchanged", () => {
    expect(checkFallbackError(429, "slow down", 0)).toEqual({ shouldFallback: true, cooldownMs: 2000, newBackoffLevel: 1 });
    expect(checkFallbackError(500, "Internal server error")).toEqual({ shouldFallback: true, cooldownMs: 30000 });
    expect(checkFallbackError(503, "upstream down")).toEqual({ shouldFallback: true, cooldownMs: 30000 });
    expect(checkFallbackError(400, "Improperly formed request.")).toEqual({ shouldFallback: true, cooldownMs: 120000 });
    expect(checkFallbackError(400, "Rate limit reached", 0)).toEqual({ shouldFallback: true, cooldownMs: 2000, newBackoffLevel: 1 });
    expect(checkFallbackError(401, "Unauthorized")).toEqual({ shouldFallback: true, cooldownMs: 120000 });
  });
});

describe("terminal 400 call sites", () => {
  it("markAccountUnavailable writes no modelLock", async () => {
    const r = await markAccountUnavailable("acct-a", 400, INVALID, "openai", "gpt-x");
    expect(r).toEqual({ shouldFallback: false, cooldownMs: 0 });
    expect(mocks.updateProviderConnection).not.toHaveBeenCalled();
  });

  it("chat returns the provider 400 after one upstream call, no second account", async () => {
    const upstream = new Response(INVALID, { status: 400, headers: { "Content-Type": "application/json" } });
    mocks.handleChatCore.mockResolvedValue({ success: false, status: 400, error: INVALID, response: upstream });
    const res = await handleChat(new Request("http://localhost/v1/chat/completions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: "openai/gpt-x", messages: [] }),
    }));
    expect(res.status).toBe(400);
    expect(await res.text()).toBe(INVALID);
    expect(mocks.handleChatCore).toHaveBeenCalledTimes(1);
    expect(mocks.updateProviderConnection).not.toHaveBeenCalled();
  });

  it("combo does not fall through to the next model on a terminal 400", async () => {
    const handleSingleModel = vi.fn(async () => new Response(INVALID, { status: 400 }));
    const log = { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() };
    const res = await handleComboChat({
      body: { messages: [] }, models: ["openai/a", "openai/b"], handleSingleModel, log,
      comboName: "terminal-400", comboStrategy: "fallback",
    });
    expect(res.status).toBe(400);
    expect(handleSingleModel).toHaveBeenCalledTimes(1);
  });
});
