/**
 * Claude usage handler
 */

import { proxyAwareFetch } from "../../utils/proxyFetch.js";
import { ANTHROPIC_API_VERSION } from "../../providers/shared.js";
import { U, parseResetTime } from "./shared.js";

// Claude API config (urls from registry, apiVersion is header logic kept here)
const CLAUDE_CONFIG = {
  oauthUsageUrl: U("claude").oauthUrl,
  usageUrl: U("claude").orgUrl,
  settingsUrl: U("claude").settingsUrl,
  apiVersion: ANTHROPIC_API_VERSION,
};

// OAuth usage endpoint rate-limits (429); cool down per-token to stop hammering it.
// Only the quota endpoint is affected — chat with the same token still works.
// Repeated 429s back off exponentially (3m, 6m, 12m, ... capped at 30m); Retry-After wins if longer.
const OAUTH_429_COOLDOWN_MS = 180000;
const OAUTH_429_MAX_COOLDOWN_MS = 1800000;
const oauthCooldown = new Map();
const oauth429Strikes = new Map();
// ponytail: last good reading per token, never evicted; tokens rotate slowly. Add LRU if accounts grow large.
const lastGood = new Map();

// Dedup + short TTL cache per access token. Many tabs / many accounts / auto-refresh
// all funnel through here; without this each call hits Anthropic and triggers 429.
const USAGE_CACHE_TTL_MS = 300000;
const usageCache = new Map(); // token -> { promise } | { result, expiresAt }

export async function getClaudeUsage(accessToken, proxyOptions = null, options = {}) {
  // Bounded callers own their request; do not join an unrelated unbounded dashboard poll.
  if (options.signal) return fetchClaudeUsageRaw(accessToken, proxyOptions, options.signal);
  const force = options?.force === true;

  // Serve in-flight or fresh cached result (skip on manual force)
  if (!force && accessToken) {
    const hit = usageCache.get(accessToken);
    if (hit?.promise) return hit.promise;
    if (hit && hit.expiresAt > Date.now()) return hit.result;
  }

  const promise = (async () => {
    const result = await fetchClaudeUsageRaw(accessToken, proxyOptions);
    if (!accessToken) return result;
    if (result?.quotas) {
      const fresh = { ...result, fetchedAt: Date.now() };
      lastGood.set(accessToken, fresh);
      usageCache.set(accessToken, { result: fresh, expiresAt: Date.now() + USAGE_CACHE_TTL_MS });
      return fresh;
    }
    // Soft failure (429/error): serve the last good read, flagged stale, instead of the error.
    const prev = lastGood.get(accessToken);
    const out = prev ? { ...prev, stale: true, staleReason: result?.message || null } : result;
    // Hold it until the cooldown ends (0 = refetch next call); never leave a settled promise cached.
    usageCache.set(accessToken, { result: out, expiresAt: oauthCooldown.get(accessToken) || 0 });
    return out;
  })();

  if (accessToken) usageCache.set(accessToken, { promise });
  return promise;
}

async function fetchClaudeUsageRaw(accessToken, proxyOptions = null, signal = undefined) {
  try {
    // Skip OAuth usage call while this token is cooling down from a recent 429.
    // Do NOT fall back to legacy: OAuth tokens lack org scope, so it always reports "admin permissions".
    const cooldownUntil = oauthCooldown.get(accessToken);
    if (cooldownUntil && Date.now() < cooldownUntil) return rateLimitedResult(cooldownUntil);

    // Primary: OAuth usage endpoint (Claude Code consumer OAuth tokens)
    const oauthResponse = await proxyAwareFetch(CLAUDE_CONFIG.oauthUsageUrl, {
      method: "GET",
      signal,
      headers: {
        "Authorization": `Bearer ${accessToken}`,
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": CLAUDE_CONFIG.apiVersion,
      },
    }, proxyOptions);

    if (oauthResponse.ok) {
      const data = await oauthResponse.json();
      const quotas = {};

      // utilization = % USED (e.g. 87 means 87% used, 13% remaining)
      const hasUtilization = (window) =>
        window && typeof window === "object" && typeof window.utilization === "number";

      const createQuotaObject = (window) => {
        const used = window.utilization;
        const remaining = Math.max(0, 100 - used);
        return {
          used,
          total: 100,
          remaining,
          remainingPercentage: remaining,
          resetAt: parseResetTime(window.resets_at),
          unlimited: false,
        };
      };

      if (hasUtilization(data.five_hour)) {
        quotas["session (5h)"] = createQuotaObject(data.five_hour);
      }

      if (hasUtilization(data.seven_day)) {
        quotas["weekly (7d)"] = createQuotaObject(data.seven_day);
      }

      // Parse model-specific weekly windows (e.g. seven_day_sonnet, seven_day_opus)
      for (const [key, value] of Object.entries(data)) {
        if (key.startsWith("seven_day_") && key !== "seven_day" && hasUtilization(value)) {
          const modelName = key.replace("seven_day_", "");
          quotas[`weekly ${modelName} (7d)`] = createQuotaObject(value);
        }
      }

      oauth429Strikes.delete(accessToken);
      return {
        plan: "Claude Code",
        extraUsage: data.extra_usage ?? null,
        quotas,
      };
    }

    // Cool down OAuth usage polling after a 429 (quota endpoint only), with exponential backoff.
    if (oauthResponse.status === 429) {
      const strikes = (oauth429Strikes.get(accessToken) || 0) + 1;
      oauth429Strikes.set(accessToken, strikes);
      const backoff = Math.min(OAUTH_429_COOLDOWN_MS * 2 ** (strikes - 1), OAUTH_429_MAX_COOLDOWN_MS);
      const retryAfterMs = Number(oauthResponse.headers?.get?.("retry-after")) * 1000;
      const until = Date.now() + Math.max(backoff, Number.isFinite(retryAfterMs) ? retryAfterMs : 0);
      oauthCooldown.set(accessToken, until);
      console.warn(`[Claude Usage] OAuth endpoint 429 (strike ${strikes}); cooling down ${Math.round((until - Date.now()) / 1000)}s`);
      return rateLimitedResult(until);
    }

    // Fallback: legacy settings + org usage endpoint
    console.warn(`[Claude Usage] OAuth endpoint returned ${oauthResponse.status}, falling back to legacy`);
    return await getClaudeUsageLegacy(accessToken, proxyOptions, signal);
  } catch (error) {
    return { message: `Claude connected. Unable to fetch usage: ${error.message}` };
  }
}

function rateLimitedResult(until) {
  // Minutes, not seconds: the usage route treats any message containing "401" as auth-expired.
  const mins = Math.max(1, Math.ceil((until - Date.now()) / 60000));
  return {
    rateLimited: true,
    retryAt: new Date(until).toISOString(),
    message: `Rate limited by Anthropic usage API; retrying in ~${mins} min.`,
  };
}

/**
 * Legacy Claude usage for API key / org admin users
 */
async function getClaudeUsageLegacy(accessToken, proxyOptions = null, signal = undefined) {
  try {
    const settingsResponse = await proxyAwareFetch(CLAUDE_CONFIG.settingsUrl, {
      method: "GET",
      signal,
      headers: {
        "Authorization": `Bearer ${accessToken}`,
        "anthropic-version": CLAUDE_CONFIG.apiVersion,
      },
    }, proxyOptions);

    if (settingsResponse.ok) {
      const settings = await settingsResponse.json();

      if (settings.organization_id) {
        const usageResponse = await proxyAwareFetch(
          CLAUDE_CONFIG.usageUrl.replace("{org_id}", settings.organization_id),
          {
            method: "GET",
      signal,
            headers: {
              "Authorization": `Bearer ${accessToken}`,
              "anthropic-version": CLAUDE_CONFIG.apiVersion,
            },
          },
          proxyOptions
        );

        if (usageResponse.ok) {
          const usage = await usageResponse.json();
          return {
            plan: settings.plan || "Unknown",
            organization: settings.organization_name,
            quotas: usage,
          };
        }
      }

      return {
        plan: settings.plan || "Unknown",
        organization: settings.organization_name,
        message: "Claude connected. Usage details require admin access.",
      };
    }

    return { message: "Claude connected. Usage API requires admin permissions." };
  } catch (error) {
    return { message: `Claude connected. Unable to fetch usage: ${error.message}` };
  }
}
