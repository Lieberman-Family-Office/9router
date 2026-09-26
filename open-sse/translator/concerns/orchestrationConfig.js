// Responses Multi-agent + server-side compaction — aligned with OpenAI docs.
// Multi-agent: multi_agent.enabled + OpenAI-Beta: responses_multi_agent=v1
// Compaction: context_management: [{ type: "compaction", compact_threshold }]
//             or POST /v1/responses/compact (standalone; incompatible with multi_agent)

export const MULTI_AGENT_BETA = "responses_multi_agent=v1";
export const DEFAULT_MAX_CONCURRENT_SUBAGENTS = 3;

/**
 * Parse model-id suffix tokens for multi-agent / compaction.
 * Tokens (comma/plus compounds with effort/mode):
 *   ma | multi | multi_agent          → multi_agent.enabled
 *   ma:N | maN | concurrent:N         → enabled + max_concurrent_subagents
 *   compact:N | compact_N | compact_Nk → context_management compaction threshold
 */
export function parseOrchestrationToken(raw) {
  const t = String(raw || "").trim().toLowerCase();
  if (!t) return null;
  if (t === "ma" || t === "multi" || t === "multi_agent") {
    return { multiAgent: { enabled: true } };
  }
  let m = t.match(/^ma:(\d+)$/) || t.match(/^concurrent:(\d+)$/) || t.match(/^ma(\d+)$/);
  if (m) {
    const n = Math.max(1, Math.floor(Number(m[1])));
    return { multiAgent: { enabled: true, max_concurrent_subagents: n } };
  }
  m = t.match(/^compact:(\d+)$/) || t.match(/^compact_(\d+)$/);
  if (m) {
    const thr = Math.floor(Number(m[1]));
    if (!(thr > 0)) return null;
    return { contextManagement: [{ type: "compaction", compact_threshold: thr }] };
  }
  m = t.match(/^compact_(\d+)k$/);
  if (m) {
    const thr = Math.floor(Number(m[1])) * 1000;
    if (!(thr > 0)) return null;
    return { contextManagement: [{ type: "compaction", compact_threshold: thr }] };
  }
  return null;
}

/** Normalize client multi_agent object; returns null when disabled/absent. */
export function normalizeMultiAgent(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (value.enabled !== true) return null;
  let max = DEFAULT_MAX_CONCURRENT_SUBAGENTS;
  if (value.max_concurrent_subagents != null) {
    const n = Number(value.max_concurrent_subagents);
    if (Number.isFinite(n) && n >= 1) max = Math.floor(n);
  }
  return { enabled: true, max_concurrent_subagents: max };
}

/** Merge body + suffix multi_agent patches (suffix can force enable / override max). */
export function mergeMultiAgent(bodyValue, suffixPatch) {
  const base = normalizeMultiAgent(bodyValue) || { enabled: false, max_concurrent_subagents: DEFAULT_MAX_CONCURRENT_SUBAGENTS };
  if (!suffixPatch || typeof suffixPatch !== "object") {
    return base.enabled ? { enabled: true, max_concurrent_subagents: base.max_concurrent_subagents } : null;
  }
  const enabled = suffixPatch.enabled === true || base.enabled;
  if (!enabled) return null;
  let max = base.max_concurrent_subagents;
  if (suffixPatch.max_concurrent_subagents != null) {
    const n = Number(suffixPatch.max_concurrent_subagents);
    if (Number.isFinite(n) && n >= 1) max = Math.floor(n);
  }
  return { enabled: true, max_concurrent_subagents: max };
}

/** Normalize context_management to compaction entries only. */
export function normalizeContextManagement(value) {
  if (!value) return null;
  const arr = Array.isArray(value) ? value : [value];
  const out = [];
  for (const item of arr) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    if (item.type !== "compaction") continue;
    const thr = Number(item.compact_threshold);
    if (!Number.isFinite(thr) || thr <= 0) continue;
    out.push({ type: "compaction", compact_threshold: Math.floor(thr) });
  }
  return out.length ? out : null;
}

/** Suffix wins when it supplies compaction; else keep body. */
export function mergeContextManagement(bodyValue, suffixEntries) {
  const fromSuffix = normalizeContextManagement(suffixEntries);
  if (fromSuffix) return fromSuffix;
  return normalizeContextManagement(bodyValue);
}

/**
 * Apply Multi-agent limitations from OpenAI docs when enabled:
 * - reasoning.summary unsupported
 * - max_tool_calls unsupported
 * Standalone /responses/compact is also unsupported (caller must gate).
 */
export function applyMultiAgentIncompatibilities(body) {
  if (!body?.multi_agent?.enabled) return body;
  if (body.reasoning && typeof body.reasoning === "object") {
    delete body.reasoning.summary;
  }
  delete body.max_tool_calls;
  return body;
}

/** Build OpenAI-Beta header value, merging client betas with multi-agent beta when needed. */
export function resolveOpenAIBetaHeader(existingHeader, body, clientBetas) {
  const parts = new Set();
  if (typeof existingHeader === "string" && existingHeader.trim()) {
    for (const p of existingHeader.split(",")) {
      const s = p.trim();
      if (s) parts.add(s);
    }
  }
  const betas = Array.isArray(clientBetas) ? clientBetas : Array.isArray(body?.betas) ? body.betas : [];
  for (const b of betas) {
    if (typeof b === "string" && b.trim()) parts.add(b.trim());
  }
  if (body?.multi_agent?.enabled) parts.add(MULTI_AGENT_BETA);
  return parts.size ? [...parts].join(", ") : null;
}
