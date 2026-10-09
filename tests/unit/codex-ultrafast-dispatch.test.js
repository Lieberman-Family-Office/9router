import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";

const mocks = vi.hoisted(() => ({ transport: vi.fn(), accounts: [], settings: {} }));
vi.mock("@/lib/localDb", () => ({
  getProviderConnections: async ({ isActive } = {}) => mocks.accounts.filter(account => isActive === undefined || account.isActive === isActive),
  getSettings: async () => mocks.settings,
  getProxyPools: async () => [], getModelAliases: async () => ({}),
  getComboByName: async () => null, getProviderNodes: async () => [],
  validateApiKey: async () => true, updateProviderConnection: async () => {},
}));
vi.mock("@/lib/usageDb.js", () => ({
  saveRequestUsage: async () => {}, saveRequestDetail: async () => {},
  appendRequestLog: async () => {}, trackPendingRequest: () => {},
}));
vi.mock("@/lib/network/connectionProxy", () => ({ resolveConnectionProxyConfig: async () => ({}), pickProxyPoolId: () => null }));
vi.mock("@/sse/services/tokenRefresh.js", () => ({ checkAndRefreshToken: async (_provider, credentials) => credentials, updateProviderCredentials: async () => {} }));
vi.mock("open-sse/utils/proxyFetch.js", () => ({ proxyAwareFetch: mocks.transport, hasEnabledProxy: () => false }));

const { handleChat } = await import("../../src/sse/handlers/chat.js");
const { initTranslators } = await import("../../open-sse/translator/index.js");
const { createResponsesWsSession } = await import("../../open-sse/handlers/responsesWs/session.js");
const { encodeTextFrame, WsFrameReader } = await import("../../open-sse/handlers/responsesWs/wsFrames.js");

beforeEach(async () => {
  vi.clearAllMocks();
  vi.stubEnv("ENABLE_REQUEST_LOGS", "false");
  mocks.settings = { quotaAwareSelection: false, codexUltrafastConnectionId: "eligible", fallbackStrategy: "fill-first" };
  mocks.accounts = [
    { id: "ordinary", provider: "codex", authType: "oauth", isActive: true, priority: 1, accessToken: "ordinary-test" },
    { id: "eligible", provider: "codex", authType: "oauth", isActive: true, priority: 2, accessToken: "eligible-test", providerSpecificData: { enabledModels: ["gpt-6-astra", "gpt-6.1-sol"], chatgptAccountId: "test-workspace" } },
  ];
  mocks.transport.mockImplementation(async (_url, options) => {
    const body = JSON.parse(options.body);
    const response = { id: "resp_test", model: body.model, status: "completed", service_tier: "ultrafast", output: [], usage: { input_tokens: 1, output_tokens: 1 } };
    const events = [
      { type: "response.created", response: { ...response, status: "in_progress" } },
      { type: "response.output_text.delta", delta: "OK" },
      { type: "response.completed", response },
    ];
    return new Response(events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(""), { headers: { "Content-Type": "text/event-stream" } });
  });
  await initTranslators();
});
afterEach(() => vi.unstubAllEnvs());

const post = body => handleChat(new Request("http://localhost/v1/responses", { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer test-client" }, body: JSON.stringify(body) }));
const assertTransport = model => {
  expect(mocks.transport).toHaveBeenCalledTimes(1);
  const [url, options] = mocks.transport.mock.calls[0];
  expect(url).toBe("https://chatgpt.com/backend-api/codex/responses");
  expect(options.headers.Authorization).toBe("Bearer eligible-test");
  expect(options.headers["ChatGPT-Account-ID"]).toBe("test-workspace");
  expect(JSON.parse(options.body)).toMatchObject({ model, service_tier: "ultrafast", reasoning: { effort: "high" } });
};

it.each(["gpt-6-astra", "gpt-6.1-sol"])("dispatches dedicated %s through real chat core and executor", async model => {
  const response = await post({ model: `cx/${model}-ultrafast(high)`, input: "OK", stream: true });
  expect(response.status).toBe(200);
  expect(await response.text()).toContain('"type":"response.completed"');
  assertTransport(model);
});

it.each(["-high-high", "()", "(bogus)", " -high", " (bogus)", "\t-xhigh"])("refuses malformed dedicated suffix %s before upstream dispatch", async suffix => {
  const response = await post({ model: `cx/gpt-6-astra-ultrafast${suffix}`, input: "OK", stream: true });
  expect(response.status).toBe(400);
  expect(mocks.transport).not.toHaveBeenCalled();
});

it("accepts max effort without changing the dedicated route", async () => {
  const response = await post({ model: "cx/gpt-6-astra-ultrafast-max", input: "OK", stream: true });
  expect(response.status).toBe(200);
  await response.text();
  const options = mocks.transport.mock.calls[0][1];
  expect(options.headers.Authorization).toBe("Bearer eligible-test");
  expect(JSON.parse(options.body)).toMatchObject({ model: "gpt-6-astra", service_tier: "ultrafast", reasoning: { effort: "max" } });
});

it.each(["audioInput", "videoInput"])("refuses unsupported %s instead of using a capacity adapter", async capability => {
  mocks.settings.capacityAdapter = { [capability]: { enabled: true, models: ["openai/gpt-6-astra"] } };
  const block = capability === "audioInput" ? { type: "input_audio", input_audio: { data: "AA==", format: "wav" } } : { type: "input_video", video_url: "data:video/mp4;base64,AA==" };
  const response = await post({ model: "cx/gpt-6-astra-ultrafast", input: [{ role: "user", content: [block] }], stream: true });
  expect(response.status).toBe(400);
  expect(await response.text()).toContain(capability);
  expect(mocks.transport).not.toHaveBeenCalled();
});

it.each(["audio/wav", "video/mp4"])("refuses unsupported native inline-file MIME %s", async mime => {
  const response = await post({ model: "cx/gpt-6-astra-ultrafast", input: [{ role: "user", content: [{ type: "input_file", file_data: `data:${mime};base64,AA==` }] }], stream: true });
  expect(response.status).toBe(400);
  expect(mocks.transport).not.toHaveBeenCalled();
});

it("translates Chat Completions through the real pinned Ultrafast executor", async () => {
  const response = await handleChat(new Request("http://localhost/v1/chat/completions", {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer test-client" },
    body: JSON.stringify({ model: "cx/gpt-6-astra-ultrafast-high", messages: [{ role: "user", content: "OK" }], stream: true }),
  }));
  expect(response.status).toBe(200);
  const text = await response.text();
  expect(text).toContain("chat.completion.chunk");
  expect(text).toContain("[DONE]");
  assertTransport("gpt-6-astra");
});

it("preserves Ultrafast model, effort, and account on a real executor retry", async () => {
  const { CodexExecutor } = await import("../../open-sse/executors/codex.js");
  const executor = new CodexExecutor();
  executor.config = { ...executor.config, retry: { 503: { attempts: 1, delayMs: 0 } } };
  mocks.transport.mockResolvedValueOnce(new Response('event: error\ndata: {"error":{"message":"server_is_overloaded"}}\n\n', { headers: { "Content-Type": "text/event-stream" } }));
  const result = await executor.execute({ model: "gpt-6-astra-ultrafast-high", body: { model: "gpt-6-astra-ultrafast-high", input: "OK" }, stream: true, credentials: { connectionId: "eligible", accessToken: "eligible-test", providerSpecificData: { chatgptAccountId: "test-workspace" } } });
  expect(await result.response.text()).toContain("response.completed");
  expect(mocks.transport).toHaveBeenCalledTimes(2);
  for (const [, options] of mocks.transport.mock.calls) {
    expect(options.headers.Authorization).toBe("Bearer eligible-test");
    expect(options.headers["ChatGPT-Account-ID"]).toBe("test-workspace");
    expect(JSON.parse(options.body)).toMatchObject({ model: "gpt-6-astra", service_tier: "ultrafast", reasoning: { effort: "high" } });
  }
});

it("preserves the dedicated route through a Responses WebSocket turn", async () => {
  const socket = new EventEmitter();
  const events = [], reader = new WsFrameReader();
  socket.destroyed = false; socket.end = vi.fn();
  socket.write = bytes => { for (const frame of reader.push(bytes)) if (frame.opcode === 1) events.push(JSON.parse(frame.payload.toString())); };
  createResponsesWsSession({ socket, req: { headers: { authorization: "Bearer test-client" } }, fetchLocalResponses: (_path, _headers, body) => post(body) });
  try {
    socket.emit("data", encodeTextFrame(JSON.stringify({ type: "response.create", model: "cx/gpt-6-astra-ultrafast-high", input: "OK", stream_id: "test-turn" }), { mask: true }));
    await vi.waitFor(() => expect(events.some(event => event.type === "response.completed")).toBe(true));
    assertTransport("gpt-6-astra");
    expect(events.find(event => event.type === "response.completed").stream_id).toBe("test-turn");
  } finally { socket.emit("close"); }
});
