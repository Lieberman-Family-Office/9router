import { getModelQuotaFamily, getModelUpstreamId } from "../../../open-sse/config/providerModels.js";

export const DEFAULT_QUOTA_CACHE_TTL_MS = 45_000;
export const DEFAULT_STALE_OK_MS = 300_000;

const PROVIDER_SESSION_KEYS = {
  claude: "session (5h)",
  codex: "session",
};

export function toFiniteNumber(value, fallback = null) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

export function isQuotaExhausted(quota) {
  if (!quota || quota.unlimited === true) return false;
  const remaining = toFiniteNumber(quota.remaining);
  if (remaining !== null) return remaining <= 0;

  const used = toFiniteNumber(quota.used);
  const total = toFiniteNumber(quota.total);
  return total !== null && total > 0 && used !== null && used >= total;
}

export function isBlockingQuotaName(name, sessionKey) {
  if (name === sessionKey) return false;
  return !String(name).toLowerCase().includes("session");
}

export function hasExhaustedBlockingQuota(quotas, sessionKey) {
  return Object.entries(quotas || {}).some(
    ([name, quota]) => isBlockingQuotaName(name, sessionKey) && isQuotaExhausted(quota),
  );
}

function fractionFromQuota(quota) {
  if (!quota) return null;
  if (quota.unlimited === true) return 1;
  const pct = toFiniteNumber(quota.remainingPercentage);
  if (pct !== null) return Math.max(0, Math.min(1, pct / 100));
  const remaining = toFiniteNumber(quota.remaining);
  const total = toFiniteNumber(quota.total);
  if (remaining !== null && total !== null && total > 0) {
    return Math.max(0, Math.min(1, remaining / total));
  }
  if (remaining !== null && total === null) {
    // Weak signal only — prefer fraction APIs; clamp large remainings.
    return Math.max(0, Math.min(1, remaining / 100));
  }
  const used = toFiniteNumber(quota.used);
  if (used !== null && total !== null && total > 0) {
    return Math.max(0, Math.min(1, (total - used) / total));
  }
  return null;
}

export function normalizeQuotasToSnapshot(providerId, usage, model = null) {
  const modelId = String(model || "").replace(/\([^()]+\)\s*$/, "").trim();
  let sessionKey = PROVIDER_SESSION_KEYS[providerId] || "session";
  let entries = Object.entries(usage?.quotas || {});
  if (providerId === "codex") {
    const family = getModelQuotaFamily("cx", modelId) === "review" ? "review"
      : getModelUpstreamId("cx", modelId) === "gpt-5.3-codex-spark" ? "spark" : "";
    sessionKey = family ? `${family}_session` : "session";
    entries = entries.filter(([name]) => [sessionKey, family ? `${family}_weekly` : "weekly"].includes(name));
  } else if (providerId === "claude") {
    entries = entries.filter(([name]) => {
      const match = /^weekly (.+) \(7d\)$/.exec(name);
      return !match || modelId.split(/[-_.]/).includes(match[1]);
    });
  }
  const quotas = Object.fromEntries(entries);
  const primary = quotas[sessionKey] || null;
  const remainingFraction = fractionFromQuota(primary);
  const exhausted = entries.filter(([name, quota]) => isBlockingQuotaName(name, sessionKey) && isQuotaExhausted(quota));
  const blockingExhausted = exhausted.length > 0;
  const resets = exhausted.map(([, quota]) => Date.parse(quota?.resetAt));
  const blockingResetAt = resets.length && resets.every(Number.isFinite)
    ? new Date(Math.max(...resets)).toISOString() : null;
  return {
    remainingFraction,
    remaining: toFiniteNumber(primary?.remaining),
    total: toFiniteNumber(primary?.total),
    resetAt: primary?.resetAt || null,
    blockingResetAt,
    unlimited: primary?.unlimited === true,
    primaryKey: primary ? sessionKey : null,
    blockingExhausted,
    unknown: remainingFraction === null && !primary,
    fetchedAt: Date.now(),
  };
}

export function compareConnectionsByRemaining(a, b) {
  const sa = a._quotaSnapshot || {};
  const sb = b._quotaSnapshot || {};
  const fa = sa.unknown ? -1 : toFiniteNumber(sa.remainingFraction, -1);
  const fb = sb.unknown ? -1 : toFiniteNumber(sb.remainingFraction, -1);
  if (fb !== fa) return fb - fa;
  const ba = toFiniteNumber(a.backoffLevel, 0);
  const bb = toFiniteNumber(b.backoffLevel, 0);
  if (ba !== bb) return ba - bb;
  const ta = a.lastUsedAt ? Date.parse(a.lastUsedAt) : 0;
  const tb = b.lastUsedAt ? Date.parse(b.lastUsedAt) : 0;
  if (ta !== tb) return ta - tb; // older first
  return String(a.id || "").localeCompare(String(b.id || ""));
}

export function sortConnectionsByRemaining(connections) {
  return [...(connections || [])].sort(compareConnectionsByRemaining);
}

export function createQuotaSnapshotCache({
  ttlMs = DEFAULT_QUOTA_CACHE_TTL_MS,
  staleOkMs = DEFAULT_STALE_OK_MS,
  // ponytail: retain at most 256 account entries; increase only for larger account pools.
  maxEntries = 256,
  now = () => Date.now(),
} = {}) {
  if (!Number.isInteger(maxEntries) || maxEntries < 1) throw new RangeError("maxEntries must be a positive integer");
  const entries = new Map();

  function get(connectionId) {
    const entry = entries.get(connectionId);
    if (entry && !entry.promise && now() - entry.touchedAt >= staleOkMs) {
      entries.delete(connectionId);
      return null;
    }
    return entry?.value || null;
  }

  function set(connectionId, value) {
    entries.delete(connectionId);
    entries.set(connectionId, { value, fingerprint: "", touchedAt: now() });
    while (entries.size > maxEntries) entries.delete(entries.keys().next().value);
  }

  async function getOrFetch(connectionId, fetcher, fingerprint = "", freshnessMs = ttlMs) {
    const t = now();
    for (const [key, entry] of entries) {
      if (!entry.promise && t - entry.touchedAt >= staleOkMs) entries.delete(key);
    }
    let entry = entries.get(connectionId);
    if (entry?.fingerprint !== fingerprint) {
      entries.delete(connectionId);
      entry = null;
    }
    const cached = entry?.value;
    if (cached && t - cached.fetchedAt < freshnessMs) return cached;
    if (entry?.promise) return entry.promise;
    entry = { fingerprint, value: cached, touchedAt: t };
    entries.delete(connectionId);
    entries.set(connectionId, entry);
    while (entries.size > maxEntries) entries.delete(entries.keys().next().value);
    entry.promise = (async () => {
      try {
        const value = await Promise.resolve().then(fetcher);
        entry.value = { ...value, fetchedAt: now(), stale: false };
        return entry.value;
      } catch {
        if (cached && now() - cached.fetchedAt < staleOkMs) return { ...cached, stale: true };
        return { unknown: true, fetchedAt: now(), stale: true };
      } finally {
        entry.promise = null;
        entry.touchedAt = now();
      }
    })();
    return entry.promise;
  }

  return { get, set, getOrFetch };
}
