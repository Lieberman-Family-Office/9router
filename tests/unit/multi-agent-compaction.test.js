import { describe, expect, it } from "vitest";
import {
  parseOrchestrationToken,
  normalizeMultiAgent,
  normalizeContextManagement,
  mergeMultiAgent,
  applyMultiAgentIncompatibilities,
  resolveOpenAIBetaHeader,
  MULTI_AGENT_BETA,
  DEFAULT_MAX_CONCURRENT_SUBAGENTS,
} from "../../open-sse/translator/concerns/orchestrationConfig.js";
import { parseSuffix, applyThinking } from "../../open-sse/translator/concerns/thinkingUnified.js";
import { CodexExecutor } from "../../open-sse/executors/codex.js";

describe("orchestrationConfig (multi-agent + compaction)", () => {
  it("parses ma / ma:N / compact tokens", () => {
    expect(parseOrchestrationToken("ma")).toEqual({ multiAgent: { enabled: true } });
    expect(parseOrchestrationToken("ma:5")).toEqual({
      multiAgent: { enabled: true, max_concurrent_subagents: 5 },
    });
    expect(parseOrchestrationToken("compact:200000")).toEqual({
      contextManagement: [{ type: "compaction", compact_threshold: 200000 }],
    });
    expect(parseOrchestrationToken("compact_200k")).toEqual({
      contextManagement: [{ type: "compaction", compact_threshold: 200000 }],
    });
  });

  it("defaults max_concurrent_subagents to 3", () => {
    expect(normalizeMultiAgent({ enabled: true })).toEqual({
      enabled: true,
      max_concurrent_subagents: DEFAULT_MAX_CONCURRENT_SUBAGENTS,
    });
  });

  it("strips reasoning.summary and max_tool_calls when multi_agent enabled", () => {
    const body = {
      multi_agent: { enabled: true, max_concurrent_subagents: 3 },
      reasoning: { effort: "high", summary: "auto" },
      max_tool_calls: 8,
    };
    applyMultiAgentIncompatibilities(body);
    expect(body.reasoning.summary).toBeUndefined();
    expect(body.max_tool_calls).toBeUndefined();
    expect(body.reasoning.effort).toBe("high");
  });

  it("sets OpenAI-Beta header value for multi-agent", () => {
    expect(
      resolveOpenAIBetaHeader(null, { multi_agent: { enabled: true } }, null),
    ).toBe(MULTI_AGENT_BETA);
  });
});

describe("model-id multi-agent / compaction", () => {
  it("parseSuffix extracts ma and compact with effort", () => {
    expect(parseSuffix("gpt-6-sol(ma:3,max)")).toMatchObject({
      cleanModel: "gpt-6-sol",
      override: { mode: "level", level: "max" },
      orchestration: {
        multiAgent: { enabled: true, max_concurrent_subagents: 3 },
      },
    });
    expect(parseSuffix("gpt-6-astra(compact:200000)")).toMatchObject({
      orchestration: {
        contextManagement: [{ type: "compaction", compact_threshold: 200000 }],
      },
    });
  });

  it("applyThinking + CodexExecutor preserve multi_agent and context_management", () => {
    const body = { model: "gpt-6-sol", input: [] };
    applyThinking("openai-responses", "gpt-6-sol(ma,compact:150000)", body, "codex");
    expect(body.multi_agent).toEqual({ enabled: true, max_concurrent_subagents: 3 });
    expect(body.context_management).toEqual([
      { type: "compaction", compact_threshold: 150000 },
    ]);

    const out = new CodexExecutor().transformRequest("gpt-6-sol", body, true, {
      providerSpecificData: {},
    });
    expect(out.multi_agent).toEqual({ enabled: true, max_concurrent_subagents: 3 });
    expect(out.context_management).toEqual([
      { type: "compaction", compact_threshold: 150000 },
    ]);
  });

  it("CodexExecutor rejects multi_agent on compact path", () => {
    const ex = new CodexExecutor();
    expect(() =>
      ex.transformRequest(
        "gpt-6-sol",
        { model: "gpt-6-sol", input: [], multi_agent: { enabled: true }, _compact: true },
        true,
        {},
      ),
    ).toThrow(/multi_agent is not supported with \/responses\/compact/);
  });

  it("mergeMultiAgent: body disabled + suffix ma enables", () => {
    expect(mergeMultiAgent({ enabled: false }, { enabled: true })).toEqual({
      enabled: true,
      max_concurrent_subagents: 3,
    });
  });

  it("normalizeContextManagement drops non-compaction entries", () => {
    expect(
      normalizeContextManagement([
        { type: "compaction", compact_threshold: 100 },
        { type: "other", compact_threshold: 50 },
      ]),
    ).toEqual([{ type: "compaction", compact_threshold: 100 }]);
  });
});
