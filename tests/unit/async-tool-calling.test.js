import { describe, expect, it } from "vitest";
import {
  openaiResponsesToOpenAIRequest,
  openaiToOpenAIResponsesRequest,
} from "../../open-sse/translator/request/openai-responses.js";
import { openaiToOpenAIResponsesResponse } from "../../open-sse/translator/response/openai-responses.js";
import { initState } from "../../open-sse/translator/index.js";
import { FORMATS } from "../../open-sse/translator/formats.js";

describe("async tool calling field preservation", () => {
  it("keeps async:true on Responses→Chat function tools and records names", () => {
    const out = openaiResponsesToOpenAIRequest("gpt-6-astra", {
      input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "weather" }] }],
      tools: [
        {
          type: "function",
          name: "get_weather",
          description: "weather",
          async: true,
          strict: true,
          parameters: {
            type: "object",
            properties: { city: { type: "string" } },
            required: ["city"],
            additionalProperties: false,
          },
        },
        {
          type: "custom",
          name: "slow_exec",
          description: "exec",
          async: true,
          format: { type: "grammar", syntax: "lark", definition: "start: /.+/" },
        },
      ],
    }, true, null);

    expect(out.tools[0].function).toMatchObject({
      name: "get_weather",
      async: true,
      strict: true,
    });
    expect(out.tools[1].function).toMatchObject({
      name: "slow_exec",
      async: true,
    });
    expect(out._asyncToolNames).toEqual(["get_weather", "slow_exec"]);
    expect(out._customToolNames).toEqual(["slow_exec"]);
  });

  it("keeps async:true on Chat→Responses function tools", () => {
    const out = openaiToOpenAIResponsesRequest("gpt-6-astra", {
      messages: [{ role: "user", content: "weather" }],
      tools: [
        {
          type: "function",
          function: {
            name: "get_weather",
            description: "weather",
            async: true,
            strict: true,
            parameters: {
              type: "object",
              properties: { city: { type: "string" } },
              required: ["city"],
              additionalProperties: false,
            },
          },
        },
      ],
    }, true, null);

    expect(out.tools).toEqual([
      {
        type: "function",
        name: "get_weather",
        description: "weather",
        parameters: {
          type: "object",
          properties: { city: { type: "string" } },
          required: ["city"],
          additionalProperties: false,
        },
        strict: true,
        async: true,
      },
    ]);
  });

  it("emits async:true on function_call stream items when tool is async", () => {
    const state = initState(FORMATS.OPENAI_RESPONSES);
    state.asyncToolNames = new Set(["get_weather"]);
    const chunks = [
      {
        id: "chatcmpl-async",
        choices: [{
          index: 0,
          delta: {
            tool_calls: [{
              index: 0,
              id: "call_weather_1",
              type: "function",
              function: { name: "get_weather", arguments: "" },
            }],
          },
          finish_reason: null,
        }],
      },
      {
        id: "chatcmpl-async",
        choices: [{
          index: 0,
          delta: {
            tool_calls: [{
              index: 0,
              function: { arguments: "{\"city\":\"Paris\"}" },
            }],
          },
          finish_reason: null,
        }],
      },
      {
        id: "chatcmpl-async",
        choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }],
      },
    ];

    const events = chunks.flatMap((chunk) => openaiToOpenAIResponsesResponse(chunk, state));
    const added = events.find((event) => event.event === "response.output_item.added");
    const done = events.find((event) => event.event === "response.output_item.done");

    expect(added.data.item).toMatchObject({
      type: "function_call",
      name: "get_weather",
      call_id: "call_weather_1",
      async: true,
    });
    expect(done.data.item).toMatchObject({
      type: "function_call",
      name: "get_weather",
      call_id: "call_weather_1",
      async: true,
      arguments: "{\"city\":\"Paris\"}",
    });
  });
});
