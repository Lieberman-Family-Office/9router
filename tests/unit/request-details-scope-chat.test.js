// handleChat opens one request scope, so every upstream attempt it makes during
// account fallback shares one requestDetails id (metadata mode: one row per request).
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getProviderConnections: vi.fn(),
  getSettings: vi.fn(),
  updateProviderConnection: vi.fn(),
  handleChatCore: vi.fn(),
  seen: [],
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

const { handleChat } = await import("@/sse/handlers/chat.js");
const { getRequestScope } = await import("@/lib/requestScope.js");

const post = () => handleChat(new Request("http://localhost/v1/chat/completions", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ model: "openai/gpt-x", messages: [] }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  mocks.handleChatCore.mockReset();
  mocks.seen = [];
  mocks.getSettings.mockResolvedValue({ quotaAwareSelection: false, fallbackStrategy: "fill-first" });
  mocks.getProviderConnections.mockResolvedValue([
    { id: "acct-a", provider: "openai", isActive: true, priority: 1, accessToken: "a" },
    { id: "acct-b", provider: "openai", isActive: true, priority: 2, accessToken: "b" },
  ]);
  const fail = { success: false, status: 401, error: "bad key", response: new Response("bad key", { status: 401 }) };
  mocks.handleChatCore.mockImplementation(async () => {
    mocks.seen.push(getRequestScope()?.id);
    return mocks.seen.length === 1 ? fail : { success: true, response: new Response("{}", { status: 200 }) };
  });
});

describe("handleChat request scope", () => {
  it("every attempt within one request sees the same scope id", async () => {
    expect((await post()).status).toBe(200);
    expect(mocks.seen).toHaveLength(2);
    expect(mocks.seen[0]).toBeTruthy();
    expect(mocks.seen[1]).toBe(mocks.seen[0]);
  });

  it("separate requests get different scope ids, and none leaks outside", async () => {
    await post();
    const first = mocks.seen[0];
    mocks.seen = [];
    await post();
    expect(mocks.seen[0]).not.toBe(first);
    expect(getRequestScope()).toBeNull();
  });
});
