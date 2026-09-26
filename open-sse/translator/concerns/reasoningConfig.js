// Responses API reasoning object — aligned with OpenAI docs (GPT-5.6 / GPT-6).
// Wire effort enum has no "ultra". Defaults: omit mode (=standard), omit context (=auto /
// model default), omit summary (opt-in only). Mid-turn effort changes use
// configuration_update input items; do not rewrite request-level reasoning.effort.

/** @type {ReadonlySet<string>} */
export const REASONING_EFFORTS = new Set([
  "none", "minimal", "low", "medium", "high", "xhigh", "max",
]);

/** @type {ReadonlySet<string>} */
export const REASONING_MODES = new Set(["pro", "standard"]);

/** @type {ReadonlySet<string>} */
export const REASONING_CONTEXTS = new Set(["auto", "current_turn", "all_turns"]);

/** @type {ReadonlySet<string>} */
export const REASONING_SUMMARIES = new Set(["auto", "concise", "detailed"]);

/**
 * Parse one model-id suffix token into a reasoning-object field patch.
 * Returns null when the token is not a reasoning-config token (effort/thinking handled elsewhere).
 *
 * Tokens:
 *   pro | standard                         → mode
 *   current_turn | all_turns | ctx_auto    → context (ctx_auto → auto)
 *   summary_auto | summary_concise | summary_detailed → summary
 */
export function parseReasoningConfigToken(raw) {
  const t = String(raw || "").trim().toLowerCase();
  if (!t) return null;
  if (REASONING_MODES.has(t)) return { mode: t };
  if (t === "current_turn" || t === "all_turns") return { context: t };
  if (t === "ctx_auto" || t === "context_auto") return { context: "auto" };
  if (t === "summary_auto") return { summary: "auto" };
  if (t === "summary_concise") return { summary: "concise" };
  if (t === "summary_detailed") return { summary: "detailed" };
  return null;
}

/** Pick documented reasoning fields from a body.reasoning object (ignore unknowns). */
export function pickReasoningConfig(reasoning) {
  if (!reasoning || typeof reasoning !== "object" || Array.isArray(reasoning)) return {};
  const out = {};
  if (typeof reasoning.effort === "string" && REASONING_EFFORTS.has(reasoning.effort.toLowerCase())) {
    out.effort = reasoning.effort.toLowerCase();
  }
  if (typeof reasoning.mode === "string" && REASONING_MODES.has(reasoning.mode.toLowerCase())) {
    out.mode = reasoning.mode.toLowerCase();
  }
  if (typeof reasoning.context === "string" && REASONING_CONTEXTS.has(reasoning.context.toLowerCase())) {
    out.context = reasoning.context.toLowerCase();
  }
  if (typeof reasoning.summary === "string" && REASONING_SUMMARIES.has(reasoning.summary.toLowerCase())) {
    out.summary = reasoning.summary.toLowerCase();
  }
  return out;
}

/**
 * Merge suffix + body reasoning config. Suffix wins per field.
 * Does not invent defaults (no forced summary/mode/context).
 */
export function mergeReasoningConfig(bodyReasoning, suffixPatch) {
  const base = pickReasoningConfig(bodyReasoning);
  const patch = suffixPatch && typeof suffixPatch === "object" ? suffixPatch : {};
  return { ...base, ...pickReasoningConfig(patch) };
}

/**
 * Apply merged reasoning config onto body.reasoning without wiping unrelated keys
 * the client may have set. Only writes documented fields that are present.
 */
export function applyReasoningConfigToBody(body, config) {
  if (!body || typeof body !== "object" || !config) return body;
  const cfg = pickReasoningConfig(config);
  if (Object.keys(cfg).length === 0) return body;
  const prev = body.reasoning && typeof body.reasoning === "object" && !Array.isArray(body.reasoning)
    ? body.reasoning
    : {};
  body.reasoning = { ...prev, ...cfg };
  return body;
}
