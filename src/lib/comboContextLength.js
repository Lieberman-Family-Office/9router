export function parseProviderModelId(id) {
  if (typeof id !== "string") return null;
  const i = id.indexOf("/");
  if (i <= 0 || i === id.length - 1) return null;
  return { alias: id.slice(0, i), modelId: id.slice(i + 1) };
}

export function comboWindowStats(modelIds, getCaps) {
  let maxContext = null;
  let minContext = null;
  let maxOutput = null;
  const unresolved = [];
  let resolved = 0;
  for (const raw of modelIds || []) {
    const parsed = parseProviderModelId(raw);
    if (!parsed) {
      unresolved.push(String(raw));
      continue;
    }
    const caps = getCaps(parsed.alias, parsed.modelId) || {};
    const cw = Number(caps.contextWindow);
    const mo = Number(caps.maxOutput);
    if (!Number.isFinite(cw) || cw <= 0) {
      unresolved.push(raw);
      continue;
    }
    resolved += 1;
    maxContext = maxContext == null ? cw : Math.max(maxContext, cw);
    minContext = minContext == null ? cw : Math.min(minContext, cw);
    if (Number.isFinite(mo) && mo > 0) {
      maxOutput = maxOutput == null ? mo : Math.max(maxOutput, mo);
    }
  }
  return { maxContext, minContext, maxOutput, resolved, unresolved };
}
