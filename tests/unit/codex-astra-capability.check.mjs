// Run: node tests/unit/codex-astra-capability.check.mjs
// Offline: actual translation and executor transformation; no execute(), fetch, or credentials.
import assert from "node:assert/strict";
import { translateRequest } from "../../open-sse/translator/index.js";
import { CodexExecutor } from "../../open-sse/executors/codex.js";
import { DEFAULT_CAPABILITIES, getCapabilitiesForModel } from "../../open-sse/providers/capabilities.js";

function translate(provider, model) {
  return translateRequest("openai-responses", "openai-responses", model, {
    model,
    input: "Reply OK.",
    reasoning: { effort: "xhigh" },
    max_output_tokens: 512,
  }, true, null, provider);
}

function codex(model) {
  return new CodexExecutor().transformRequest(model, translate("codex", model), true, {});
}

const astra = codex("gpt-6-astra");
assert.equal(astra.reasoning.effort, "xhigh");
assert.equal(astra.model, "gpt-6-astra");
assert.equal(astra.store, false);
assert.equal(astra.stream, true);
assert.ok(!("reasoning_effort" in astra));
assert.ok(!("max_output_tokens" in astra)); // Existing executor allowlist, not a budget fix.
assert.deepEqual(getCapabilitiesForModel("codex", "gpt-6-astra"), {
  ...DEFAULT_CAPABILITIES, reasoning: true, thinkingFormat: "openai",
});

// Six controls: existing Codex reasoning, unknown Codex, and unrelated provider/model scopes.
assert.equal(codex("gpt-5.3-codex").reasoning.effort, "xhigh");
assert.equal(codex("unknown-capability-control").reasoning.effort, "low");
for (const [provider, model] of [
  ["openai", "gpt-6-astra"],
  ["openai-compatible-responses-offline", "gpt-6-astra"],
  ["codex", "gpt-6-other"],
  ["kiro", "gpt-6-astra"],
]) {
  assert.equal(getCapabilitiesForModel(provider, model).reasoning, false);
  const body = translate(provider, model);
  assert.ok(!body.reasoning && !body.reasoning_effort);
  assert.equal(body.max_output_tokens, 512);
}
console.log("PASS: Codex Astra xhigh; six unchanged controls; zero inference requests");
