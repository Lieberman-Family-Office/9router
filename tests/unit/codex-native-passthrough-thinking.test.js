import { beforeEach, describe, expect, it, vi } from "vitest";

const { executeMock, forcedSSEToJsonMock } = vi.hoisted(() => ({
  executeMock: vi.fn(),
  forcedSSEToJsonMock: vi.fn(),
}));

vi.mock("../../open-sse/executors/index.js", () => ({
  getExecutor: () => ({
    noAuth: true,
    execute: executeMock,
  }),
}));

vi.mock("../../open-sse/utils/requestLogger.js", () => ({
  createRequestLogger: async () => ({
    logClientRawRequest: vi.fn(),
    logRawRequest: vi.fn(),
    logTargetRequest: vi.fn(),
    logProviderResponse: vi.fn(),
    logConvertedResponse: vi.fn(),
    logError: vi.fn(),
  }),
}));

vi.mock("@/lib/usageDb.js", () => ({
  trackPendingRequest: vi.fn(),
  appendRequestLog: vi.fn(async () => {}),
  saveRequestDetail: vi.fn(async () => {}),
}));

vi.mock("../../open-sse/handlers/chatCore/sseToJsonHandler.js", () => ({
  handleForcedSSEToJson: forcedSSEToJsonMock,
}));

const { handleChatCore } = await import("../../open-sse/handlers/chatCore.js");

async function runNativeCodexRequest(model, reasoning) {
  const body = {
    model,
    input: "hello",
    stream: false,
    ...(reasoning ? { reasoning } : {}),
  };

  await handleChatCore({
    body,
    modelInfo: { provider: "codex", model },
    credentials: { accessToken: "test-token", providerSpecificData: {} },
    log: { debug: vi.fn(), info: vi.fn(), warn: vi.fn() },
    connectionId: "test-connection",
    rtkEnabled: false,
    headroomEnabled: false,
    cavemanEnabled: false,
    ponytailEnabled: false,
    pxpipeEnabled: false,
    sourceFormatOverride: "openai-responses",
    clientRawRequest: {
      endpoint: "/v1/responses",
      body,
      headers: {
        accept: "application/json",
        "user-agent": "codex-cli/0.144.1",
      },
    },
  });

  return executeMock.mock.calls.at(-1)[0].body;
}

describe("native Codex passthrough thinking suffixes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    executeMock.mockResolvedValue({
      response: new Response("", { status: 200 }),
      url: "https://chatgpt.com/backend-api/codex/responses",
      headers: {},
      transformedBody: null,
    });
    forcedSSEToJsonMock.mockResolvedValue({
      success: true,
      response: new Response("{}", { status: 200 }),
    });
  });

  it("forwards max effort for Sol", async () => {
    const body = await runNativeCodexRequest("gpt-5.6-sol(max)");

    expect(body.model).toBe("gpt-5.6-sol");
    expect(body.reasoning).toEqual({ effort: "max" });
  });

  it("forwards pro reasoning mode from model suffix onto pre-executor body", async () => {
    const body = await runNativeCodexRequest("gpt-6-astra(pro)", {
      effort: "high",
      summary: "detailed",
    });

    expect(body.model).toBe("gpt-6-astra");
    // applyThinking still sets mode=pro; CodexExecutor.transformRequest strips it upstream.
    expect(body.reasoning).toEqual({ effort: "high", summary: "detailed", mode: "pro" });
  });

  it("forwards max,pro compound through a Terra review alias", async () => {
    const body = await runNativeCodexRequest("gpt-5.6-terra-review(max,pro)", {
      effort: "low",
    });

    expect(body.model).toBe("gpt-5.6-terra");
    expect(body.reasoning).toEqual({ effort: "max", mode: "pro" });
  });
});
