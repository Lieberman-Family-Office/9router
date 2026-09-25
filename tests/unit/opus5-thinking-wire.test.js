// Claude Opus 5 wire shape: every effort maps onto Anthropic's enum
// (low|medium|high|xhigh|max), and thinking text is requested, not omitted.
import { describe, expect, it } from "vitest";
import { translateRequest } from "../../open-sse/translator/index.js";
import { getThinkingLevels } from "../../open-sse/providers/thinkingLevels.js";
import { selectAnthropicBeta } from "../../open-sse/providers/shared.js";
import { claudeToOpenAIResponse } from "../../open-sse/translator/response/claude-to-openai.js";

const MODEL = "claude-opus-5-5";

function wire(model, extra = {}, source = "openai") {
  const body = { model, max_tokens: 64000, messages: [{ role: "user", content: "hi" }], ...extra };
  const out = translateRequest(source, "claude", model, body, false, {}, "claude");
  return { thinking: out.thinking, output_config: out.output_config };
}

describe("Opus 5 effort reaches Anthropic unchanged or mapped onto its enum", () => {
  it("offers xhigh in the level picker", () => {
    expect(getThinkingLevels("claude", MODEL)).toEqual(["none", "low", "medium", "high", "xhigh", "max"]);
  });

  const cases = { low: "low", medium: "medium", high: "high", xhigh: "xhigh", max: "max", ultra: "max", minimal: "low" };
  for (const [asked, sent] of Object.entries(cases)) {
    it(`${asked} -> ${sent} (suffix and reasoning_effort)`, () => {
      expect(wire(`${MODEL}(${asked})`).output_config).toEqual({ effort: sent });
      expect(wire(MODEL, { reasoning_effort: asked }).output_config).toEqual({ effort: sent });
    });
  }

  it("auto sends no effort (API default), never the invalid value 'auto'", () => {
    expect(wire(`${MODEL}(auto)`).output_config).toBeUndefined();
  });
});

describe("Opus 5 thinking is on and its text is visible", () => {
  it("adds display summarized when an effort is requested", () => {
    expect(wire(`${MODEL}(high)`).thinking).toEqual({ type: "adaptive", display: "summarized" });
  });

  it("turns on adaptive thinking with display summarized when nothing is requested", () => {
    expect(wire(MODEL)).toEqual({ thinking: { type: "adaptive", display: "summarized" }, output_config: undefined });
  });

  it("keeps a Claude-format client's explicit display choice", () => {
    const t = wire(MODEL, { thinking: { type: "adaptive", display: "omitted" } }, "claude").thinking;
    expect(t).toEqual({ type: "adaptive", display: "omitted" });
  });

  it("Claude-format client without display gets summarized", () => {
    expect(wire(MODEL, { output_config: { effort: "xhigh" } }, "claude"))
      .toEqual({ thinking: { type: "adaptive", display: "summarized" }, output_config: { effort: "xhigh" } });
  });

  it("still lets a client turn thinking off", () => {
    expect(wire(`${MODEL}(none)`).thinking).toEqual({ type: "disabled" });
  });

  it("does not ask Anthropic to redact thinking (it blanks the text even with display summarized)", () => {
    for (const m of ["claude-opus-5-5", "claude-haiku-4-5"]) {
      expect(selectAnthropicBeta(m)).not.toMatch(/redact-thinking/);
    }
  });

  it("streams thinking as reasoning_content, with no <think> tags in the answer", () => {
    const state = { toolCalls: new Map(), toolCallIndex: 0 };
    const events = [
      { type: "message_start", message: { id: "m1", model: "claude-opus-5-5" } },
      { type: "content_block_start", index: 0, content_block: { type: "thinking", thinking: "" } },
      { type: "content_block_delta", index: 0, delta: { type: "thinking_delta", thinking: "Let me check." } },
      { type: "content_block_stop", index: 0 },
      { type: "content_block_start", index: 1, content_block: { type: "text", text: "" } },
      { type: "content_block_delta", index: 1, delta: { type: "text_delta", text: "Answer." } },
      { type: "content_block_stop", index: 1 },
    ];
    const deltas = events.flatMap((e) => claudeToOpenAIResponse(e, state) || []).map((c) => c.choices[0].delta);
    expect(deltas.map((d) => d.reasoning_content || "").join("")).toBe("Let me check.");
    expect(deltas.map((d) => d.content || "").join("")).toBe("Answer.");
  });

  it("non-Claude-5 adaptive models get no thinking added when none requested", () => {
    expect(wire("claude-opus-4-8").thinking).toBeUndefined();
  });
});
