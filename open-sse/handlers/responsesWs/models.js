/**
 * GPT-6 family detection for mid-turn steering eligibility.
 * OpenAI docs: steering is available for GPT-6 over Responses WebSocket;
 * GPT-5.6 and earlier do not support it.
 */

const GPT6_BASE = /(?:^|[\/.])gpt-6(?:-|$)/i;

/**
 * Strip common 9router prefixes/suffixes (cx/, provider aliases, effort suffixes).
 * @param {string} model
 * @returns {string}
 */
export function normalizeModelId(model) {
  let m = String(model || "").trim();
  // provider aliases: cx/gpt-6-astra, openai/gpt-6-sol, codex/gpt-6-astra
  m = m.replace(/^(?:cx|codex|openai|oai)\//i, "");
  // effort / review suffixes commonly appended by 9router
  m = m.replace(/-review$/i, "");
  m = m.replace(/-(?:none|minimal|low|medium|high|xhigh|max|ultra)$/i, "");
  return m;
}

/**
 * @param {string} model
 * @returns {boolean}
 */
export function isGpt6Family(model) {
  const id = normalizeModelId(model);
  return GPT6_BASE.test(id) || /^gpt-6-/i.test(id);
}

/**
 * Mid-turn steering requires GPT-6 family.
 * @param {string} model
 * @returns {boolean}
 */
export function modelSupportsSteering(model) {
  return isGpt6Family(model);
}

/**
 * Routes that cannot proxy upstream WS steering (ChatGPT Codex HTTP-only backend).
 * Local 9router can still accept WS and implement steering as HTTP continuations,
 * but native upstream `response.steer` is unavailable.
 * @param {string} provider
 * @returns {"openai_ws"|"local_continuation"|"unsupported"}
 */
export function steerUpstreamMode(provider) {
  const p = String(provider || "").toLowerCase();
  if (p === "openai" || p === "openai-compatible") return "openai_ws";
  if (p === "codex" || p === "cx") return "local_continuation";
  return "unsupported";
}
