// GPT-6 on Codex: astra/sol share low|medium|high|xhigh|max (no ultra).
// Cost lookup for usage logged with an effort label.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { getThinkingLevels } from "../../open-sse/providers/thinkingLevels.js";
import { applyThinking } from "../../open-sse/translator/concerns/thinkingUnified.js";
import { CodexExecutor } from "../../open-sse/executors/codex.js";

function sentEffort(model, requested) {
  const body = { model, reasoning_effort: requested, input: [] };
  applyThinking("openai-responses", `${model}(${requested})`, body, "codex");
  return new CodexExecutor().transformRequest(model, body, true, { providerSpecificData: {} }).reasoning?.effort;
}

describe("GPT-6 Codex effort levels", () => {
  it("gpt-6-astra and gpt-6-sol share identical effort levels (no ultra)", () => {
    const want = ["low", "medium", "high", "xhigh", "max"];
    expect(getThinkingLevels("codex", "gpt-6-astra")).toEqual(want);
    expect(getThinkingLevels("codex", "gpt-6-sol")).toEqual(want);
  });

  for (const model of ["gpt-6-astra", "gpt-6-sol"]) {
    it(`${model}: low/medium/high/xhigh/max pass through; ultra is dropped (not remapped)`, () => {
      for (const level of ["low", "medium", "high", "xhigh", "max"]) {
        expect(sentEffort(model, level)).toBe(level);
      }
      // ultra is not a wire effort — ignored, falls back to default low
      expect(sentEffort(model, "ultra")).toBe("low");
    });
  }
});

describe("pricing for effort-labelled usage", () => {
  const saved = process.env.DATA_DIR;
  let dir, pricingRepo;
  beforeAll(async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "9r-gpt6-pricing-"));
    process.env.DATA_DIR = dir;
    vi.resetModules();
    const db = await import("@/lib/db/index.js");
    await db.initDb();
    await db.updatePricing({ codex: { "gpt-6-astra": { input: 10, output: 50, cached: 1, reasoning: 50, cache_creation: 12.5 } } });
    pricingRepo = await import("@/lib/db/repos/pricingRepo.js");
  });
  afterAll(() => {
    fs.rmSync(dir, { recursive: true, force: true });
    if (saved === undefined) delete process.env.DATA_DIR; else process.env.DATA_DIR = saved;
  });

  it("a user price for the base model also prices its effort-labelled variants", async () => {
    for (const m of ["gpt-6-astra", "gpt-6-astra(max)", "gpt-6-astra(xhigh)"]) {
      expect(await pricingRepo.getPricingForModel("codex", m)).toMatchObject({ input: 10, output: 50 });
    }
  });

  it("gpt-6-sol has a built-in price, with or without an effort label", async () => {
    for (const m of ["gpt-6-sol", "gpt-6-sol(max)", "gpt-6-sol(xhigh)"]) {
      expect(await pricingRepo.getPricingForModel("codex", m)).toMatchObject({ input: 2, output: 10 });
    }
  });

  it("an unknown model is still unpriced (no accidental match)", async () => {
    expect(await pricingRepo.getPricingForModel("codex", "totally-unknown(max)")).toBeNull();
  });
});
