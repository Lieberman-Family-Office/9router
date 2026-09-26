/**
 * ChatGPT Codex OAuth only accepts a subset of model ids.
 * OpenAI Platform / Codex-CLI defaults (gpt-5, gpt-5.4, gpt-4o, o3, …) 400 with:
 *   "The '…' model is not supported when using Codex with a ChatGPT account."
 *
 * Allowlist verified live against ChatGPT OAuth (2026-09-26). Registry entries
 * that still 400 (e.g. gpt-5.4) are intentionally omitted.
 */
export const CODEX_CHATGPT_DEFAULT_MODEL = "gpt-5.5";

const CODEX_CHATGPT_ALLOWED = new Set([
  "gpt-6-sol",
  "gpt-6-astra",
  "gpt-5.6-sol",
  "gpt-5.6-terra",
  "gpt-5.6-luna",
  "gpt-5.5",
  "gpt-5.3-codex-spark",
]);

/** Strip effort suffix `gpt-6-sol(high)` and `-review` quota variants. */
function normalizeCodexModelId(model) {
  if (!model || typeof model !== "string") return "";
  return model.replace(/\(.*\)$/, "").replace(/-review$/, "").trim();
}

/**
 * Map a client model id to one ChatGPT Codex OAuth accepts.
 * Returns `{ model, remappedFrom }` when changed, else `{ model, remappedFrom: null }`.
 */
export function resolveCodexChatGptModel(model) {
  const normalized = normalizeCodexModelId(model);
  if (!normalized) {
    return { model: CODEX_CHATGPT_DEFAULT_MODEL, remappedFrom: model || null };
  }
  if (CODEX_CHATGPT_ALLOWED.has(normalized)) {
    return { model, remappedFrom: null };
  }
  return { model: CODEX_CHATGPT_DEFAULT_MODEL, remappedFrom: model };
}
