// Resolve valid thinking levels per model — drives UI level picker (suffix "model(level)").
// Reuses capabilities.js (thinkingFormat/canDisable) so this file only maps format→levels (DRY).
import { getCapabilitiesForModel } from "./capabilities.js";
import { matchPattern } from "./pricing.js";
import { resolveKiroEffortPath } from "../config/kiroConstants.js";

// Shared level sets (deduped) — verified against provider docs + wire in thinkingUnified.applyFormat.
const L = {
  base: ["none", "low", "medium", "high"],                          // qwen, step, hunyuan, gemini-budget
  onOff: ["none", "thinking"],                                      // zai (binary), minimax (adaptive)
  openai: ["none", "minimal", "low", "medium", "high", "xhigh"],    // GPT-5.x / o-series (no "max")
  levelMax: ["none", "low", "medium", "high", "max"],               // claude-adaptive, kimi
  budgetX: ["none", "low", "medium", "high", "xhigh", "max"],       // claude-budget
  gemini: ["minimal", "low", "medium", "high"],                     // gemini-3 thinkingLevel (no disable)
  hiMax: ["none", "high", "max"],                                   // deepseek (low/med→high, xhigh→max)
};

// thinkingFormat → valid selectable levels (source of truth for UI options).
const FORMAT_LEVELS = {
  openai: L.openai,
  "claude-adaptive": L.levelMax,
  "claude-budget": L.budgetX,
  "gemini-level": L.gemini,
  "gemini-budget": L.base,
  zai: L.onOff,
  qwen: L.base,
  kimi: L.levelMax,
  deepseek: L.hiMax,
  minimax: L.onOff,
  hunyuan: L.base,
  step: L.base,
};

const CODEX_GPT_5_6_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];

// Model-name pattern overrides (glob, first match wins) — more precise than format default.
const PATTERN_THINKING = [
  // GPT-6: measured live 2026-09-26 against chatgpt.com/backend-api/codex —
  // "max" accepted; "ultra" rejected 400
  // ("Supported values are: 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', and 'max'.").
  // Client "ultra" remaps via resolveCodexClientUltraEffort (Codex CLI models.json):
  // gpt-6-astra → xhigh, gpt-6-sol → max.
  { provider: "codex", pattern: "*gpt-6-astra*", levels: CODEX_GPT_5_6_LEVELS },
  { provider: "codex", pattern: "*gpt-6-sol*", levels: CODEX_GPT_5_6_LEVELS },
  { provider: "codex", pattern: "*gpt-5.6-sol*", levels: [...CODEX_GPT_5_6_LEVELS, "ultra"] },
  { provider: "codex", pattern: "*gpt-5.6-terra*", levels: [...CODEX_GPT_5_6_LEVELS, "ultra"] },
  { provider: "codex", pattern: "*gpt-5.6-luna*", levels: CODEX_GPT_5_6_LEVELS },
  // Claude Opus 5: API accepts low/medium/high/xhigh/max (platform.claude.com effort docs).
  { provider: "claude", pattern: "*claude*opus-5*", levels: ["none", "low", "medium", "high", "xhigh", "max"] },
  { pattern: "*codex*", levels: ["low", "medium", "high", "xhigh"] }, // codex cannot disable thinking
];

// Returns valid thinking levels for a model, or null when the model has no reasoning.
export function getThinkingLevels(provider, model) {
  if (provider === "kiro" && resolveKiroEffortPath(model) === null) return null;
  const caps = getCapabilitiesForModel(provider, model);
  if (!caps.reasoning) return null;
  const hit = PATTERN_THINKING.find((entry) =>
    (!entry.provider || entry.provider === provider) && matchPattern(entry.pattern, model)
  );
  let levels = hit?.levels || FORMAT_LEVELS[caps.thinkingFormat] || L.base;
  if (caps.thinkingCanDisable === false) levels = levels.filter((l) => l !== "none");
  return levels;
}

/**
 * Map client "ultra" to a wire effort Codex accepts for this model.
 * Matches Codex CLI app-server models.json effort remapping (not Multi-Agent V2).
 * Wire never gets "ultra" for gpt-6-astra / gpt-6-sol (ChatGPT rejects it).
 */
export function resolveCodexClientUltraEffort(model) {
  const m = String(model || "").toLowerCase();
  if (m.includes("gpt-6-astra")) return "xhigh";
  if (m.includes("gpt-6-sol")) return "max";
  // Codex CLI mare null → "max" for other models that lack a wire ultra.
  return "max";
}
