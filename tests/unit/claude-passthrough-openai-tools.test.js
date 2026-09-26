/**
 * Regression: Claude Code UA + OpenAI-shaped body must NOT native-passthrough
 * to the Claude provider. Combo→Mac hops (and OpenAI clients with a Claude
 * account) arrive with User-Agent claude-cli but tools like
 * `{ type: "function", function: {…} }`. Anthropic's Messages API rejects
 * that tag with HTTP 400:
 *   Input tag 'function' found using 'type' does not match any of the expected tags…
 *
 * Measured 2026-09-26 on Niteshift → cc/claude-opus-5-5 (openai passthrough).
 */

import { describe, it, expect, beforeEach, vi } from "vitest";

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

vi.mock("../../open-sse/handlers/chatCore/nonStreamingHandler.js", () => ({
  handleNonStreamingResponse: vi.fn(async () => ({
    success: true,
    response: new Response("{}", { status: 200 }),
  })),
}));

import { isNativePassthrough, detectClientTool } from "../../open-sse/utils/clientDetector.js";
import { defaultClaudeToolType } from "../../open-sse/translator/concerns/toolCall.js";
import { handleChatCore } from "../../open-sse/handlers/chatCore.js";

describe("isNativePassthrough format gate", () => {
  it("allows Claude→Claude when body is already Claude format", () => {
    expect(isNativePassthrough("claude", "claude", "claude")).toBe(true);
  });

  it("refuses Claude→Claude when body is OpenAI format", () => {
    expect(isNativePassthrough("claude", "claude", "openai")).toBe(false);
  });

  it("keeps prior behavior when sourceFormat omitted", () => {
    expect(isNativePassthrough("claude", "claude")).toBe(true);
  });

  it("detects Claude Code UA", () => {
    expect(
      detectClientTool({ "user-agent": "claude-cli/2.1.92 (external, sdk-cli)" }, {})
    ).toBe("claude");
  });
});

describe("defaultClaudeToolType OpenAI function rewrite", () => {
  it("rewrites type:function into Anthropic custom tools", () => {
    const out = defaultClaudeToolType([
      {
        type: "function",
        function: {
          name: "lookup",
          description: "probe",
          parameters: { type: "object", properties: {} },
        },
      },
    ]);
    expect(out).toEqual([
      {
        type: "custom",
        name: "lookup",
        description: "probe",
        input_schema: { type: "object", properties: {} },
      },
    ]);
  });

  it("leaves Anthropic built-in tool types alone", () => {
    const tool = { type: "web_search_20250305", name: "web_search" };
    expect(defaultClaudeToolType([tool])).toEqual([tool]);
  });
});

describe("Claude UA + OpenAI tools: translate instead of passthrough", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    executeMock.mockResolvedValue({
      response: new Response("{}", { status: 200 }),
      url: "https://api.anthropic.com/v1/messages?beta=true",
      headers: {},
      transformedBody: null,
    });
    forcedSSEToJsonMock.mockResolvedValue({
      success: true,
      response: new Response("{}", { status: 200 }),
    });
  });

  it("translates OpenAI function tools to Anthropic custom before Claude upstream", async () => {
    const body = {
      model: "cc/claude-opus-5-5",
      stream: true,
      messages: [{ role: "user", content: "hi" }],
      tools: [
        {
          type: "function",
          function: {
            name: "lookup",
            description: "probe",
            parameters: { type: "object", properties: {} },
          },
        },
      ],
      tool_choice: "auto",
    };

    await handleChatCore({
      body,
      modelInfo: { provider: "claude", model: "claude-opus-5-5" },
      credentials: { accessToken: "test-token", connectionName: "jdh849@gmail.com" },
      log: {
        debug: vi.fn(),
        info: vi.fn(),
        warn: vi.fn(),
        line: vi.fn(),
        tagForSession: () => "t",
        fmtThink: () => null,
      },
      connectionId: "test-connection",
      rtkEnabled: false,
      headroomEnabled: false,
      cavemanEnabled: false,
      ponytailEnabled: false,
      pxpipeEnabled: false,
      clientRawRequest: {
        endpoint: "/v1/chat/completions",
        body,
        headers: {
          "user-agent": "claude-cli/2.1.92 (external, sdk-cli)",
          "x-app": "cli",
        },
      },
    });

    expect(executeMock).toHaveBeenCalled();
    const upstream = executeMock.mock.calls.at(-1)[0].body;
    expect(upstream.tools).toHaveLength(1);
    expect(upstream.tools[0].type).toBe("custom");
    expect(upstream.tools[0].name).toBe("lookup");
    expect(upstream.tools[0]).not.toHaveProperty("function");
    expect(upstream.tools[0].type).not.toBe("function");
  });
});
