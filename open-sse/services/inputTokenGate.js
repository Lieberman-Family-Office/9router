export const CHARS_PER_TOKEN = 4;

export function estimateTokensFromJsonString(s) {
  return Math.floor(String(s || "").length / CHARS_PER_TOKEN);
}

export function shouldRefuse(estimated, maxTokens) {
  const max = Number(maxTokens);
  if (!Number.isFinite(max) || max <= 0) return false;
  return estimated > max;
}
