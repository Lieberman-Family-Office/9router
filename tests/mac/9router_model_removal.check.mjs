// Run with: node tests/mac/9router_model_removal.check.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { PROVIDER_MODELS, isValidModel } from "../../open-sse/config/providerModels.js";
import * as schema from "../../open-sse/providers/models/schema.js";
import * as pricing from "../../open-sse/providers/pricing.js";

const removed = [
  "gpt-5.4-mini", "cx/gpt-5.4-mini-review", "openai/gpt-5.4-mini(high)",
  "windsurf/gpt-5-4-mini-xhigh", "gpt-5.4-mini(compact_200k)",
  "openai/gpt-5.4-mini-2026-03-17", " GPT-5.4-MINI[1m] ", "openai/gpt-5.4-mini:free",
];
const retired = /gpt[- ]5[.-]4[- ]mini\b/i;
const models = Object.values(PROVIDER_MODELS).flat();
assert(models.length > 0);
assert.deepEqual(models.filter(model => retired.test(model.id)), [], "Retired model remains in registry");
assert(!retired.test(JSON.stringify(pricing)), "Retired model remains in pricing");
for (const file of ["9router_deploy.py", "9router_vm_qualify.py"]) {
  const text = readFileSync(new URL(`../../scripts/mac/${file}`, import.meta.url), "utf8");
  assert(!text.includes("gpt-5.4-mini"), `${file} retains the retired probe`);
  assert(text.includes("cx/gpt-6-astra"), `${file} lacks the replacement probe`);
}
for (const model of removed) {
  assert.equal(schema.isRemovedModel(model), true, model);
  assert.equal(isValidModel("cx", model, new Set(["cx"])), false, model);
}
for (const model of ["gpt-5.4", "gpt-5.4-nano", "gpt-5.1-codex-mini", "gpt-5.4-miniature", "cx/gpt-6-astra", "cc/claude-haiku-4-5-20251001", null]) {
  assert.equal(schema.isRemovedModel(model), false, String(model));
}
assert.equal(isValidModel("cx", "gpt-6-astra"), true);
console.log(`Model removal check: ${models.length} registry entries; retired variants rejected; neighboring models preserved.`);
