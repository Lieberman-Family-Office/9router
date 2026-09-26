import { describe, expect, it } from "vitest";
import { applyThinking, parseSuffix } from "../../open-sse/translator/concerns/thinkingUnified.js";
import { pickReasoningConfig } from "../../open-sse/translator/concerns/reasoningConfig.js";
import { CodexExecutor } from "../../open-sse/executors/codex.js";

function wire(modelId, body = {}) {
  const base = modelId.replace(/\([^()]*\)\s*$/, "");
  const b = { model: base, input: [], ...body };
  applyThinking("openai-responses", modelId, b, "codex");
  return new CodexExecutor().transformRequest(b.model, b, true, { providerSpecificData: {} });
}

describe("Responses reasoning config (OpenAI docs)", () => {
  it("parseSuffix compounds: effort + mode + context + summary", () => {
    expect(parseSuffix("gpt-6-astra(max,pro,all_turns,summary_auto)")).toEqual({
      cleanModel: "gpt-6-astra",
      override: { mode: "level", level: "max" },
      reasoningConfig: { mode: "pro", context: "all_turns", summary: "auto" },
      reasoningMode: "pro",
      orchestration: null,
      textConfig: null,
    });
  });

  it("rejects ultra (not in wire effort enum)", () => {
    expect(parseSuffix("gpt-6-sol(ultra)")).toEqual({
      cleanModel: "gpt-6-sol",
      override: null,
      reasoningConfig: null,
      reasoningMode: null,
      orchestration: null,
      textConfig: null,
    });
  });

  it("does not force summary:auto (opt-in only)", () => {
    const out = wire("gpt-6-astra(high)");
    expect(out.reasoning.effort).toBe("high");
    expect(out.reasoning.summary).toBeUndefined();
    expect(out.reasoning.mode).toBeUndefined();
    expect(out.reasoning.context).toBeUndefined();
  });

  it("sends context+summary from model id; strips mode=pro on Codex OAuth", () => {
    const out = wire("gpt-6-astra(max,pro,all_turns,summary_auto)");
    expect(out.model).toBe("gpt-6-astra");
    expect(out.reasoning).toEqual({
      effort: "max",
      context: "all_turns",
      summary: "auto",
    });
  });

  it("preserves body reasoning.context when suffix only sets pro (mode stripped on Codex)", () => {
    const out = wire("gpt-6-sol(pro)", {
      reasoning: { effort: "medium", context: "current_turn" },
    });
    expect(out.reasoning).toMatchObject({
      effort: "medium",
      context: "current_turn",
    });
    expect(out.reasoning.mode).toBeUndefined();
  });

  it("pickReasoningConfig ignores undocumented fields", () => {
    expect(pickReasoningConfig({ effort: "high", mode: "pro", foo: 1, context: "all_turns" })).toEqual({
      effort: "high",
      mode: "pro",
      context: "all_turns",
    });
  });
});
