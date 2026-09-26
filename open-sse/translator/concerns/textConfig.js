// Responses API text object — aligned with OpenAI deployment checklist (text.verbosity).
// Values: low | medium | high. Default when omitted is medium (model/API default).
// Do not invent a default in the router — omit the field unless the client or model-id opts in.
// Chat Completions expose the same enum as top-level `verbosity`; we map both shapes.

/** @type {ReadonlySet<string>} */
export const TEXT_VERBOSITIES = new Set(["low", "medium", "high"]);

/**
 * Parse one model-id suffix token into a text-config patch.
 * Tokens (do not collide with effort low/medium/high):
 *   verbosity_low | verbosity_medium | verbosity_high
 *   verb_low | verb_medium | verb_high
 */
export function parseTextConfigToken(raw) {
  const t = String(raw || "").trim().toLowerCase();
  if (!t) return null;
  let m = t.match(/^verbosity_(low|medium|high)$/) || t.match(/^verb_(low|medium|high)$/);
  if (m) return { verbosity: m[1] };
  return null;
}

/** Pick documented text fields from body.text and/or Chat Completions body.verbosity. */
export function pickTextConfig(source) {
  if (!source || typeof source !== "object" || Array.isArray(source)) return {};
  const out = {};
  // Chat Completions: top-level verbosity
  if (typeof source.verbosity === "string" && TEXT_VERBOSITIES.has(source.verbosity.toLowerCase())) {
    out.verbosity = source.verbosity.toLowerCase();
  }
  // Responses: text.verbosity (source may be body or body.text)
  const text = source.text && typeof source.text === "object" && !Array.isArray(source.text)
    ? source.text
    : source;
  if (typeof text.verbosity === "string" && TEXT_VERBOSITIES.has(text.verbosity.toLowerCase())) {
    out.verbosity = text.verbosity.toLowerCase();
  }
  return out;
}

/** Merge body + suffix text config. Suffix wins per field. No invented defaults. */
export function mergeTextConfig(bodySource, suffixPatch) {
  const base = pickTextConfig(bodySource);
  const patch = suffixPatch && typeof suffixPatch === "object" ? suffixPatch : {};
  return { ...base, ...pickTextConfig(patch) };
}

/**
 * Apply merged text config onto body.text.verbosity without wiping other text keys
 * (format / json_schema). Also mirrors to top-level body.verbosity for Chat Completions clients.
 */
export function applyTextConfigToBody(body, config) {
  if (!body || typeof body !== "object" || !config) return body;
  const cfg = pickTextConfig(config);
  if (!cfg.verbosity) return body;
  const prev = body.text && typeof body.text === "object" && !Array.isArray(body.text)
    ? body.text
    : {};
  body.text = { ...prev, verbosity: cfg.verbosity };
  body.verbosity = cfg.verbosity;
  return body;
}
