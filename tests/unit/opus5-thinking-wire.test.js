// Claude Opus 5 wire shape: every effort maps onto Anthropic's enum
// (low|medium|high|xhigh|max), and thinking text is requested, not omitted.
import { describe, expect, it } from "vitest";
import { translateRequest } from "../../open-sse/translator/index.js";
import { getThinkingLevels } from "../../open-sse/providers/thinkingLevels.js";

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

  it("non-Claude-5 adaptive models get no thinking added when none requested", () => {
    expect(wire("claude-opus-4-8").thinking).toBeUndefined();
  });
});
