/**
 * Claude usage handler
 */

import { proxyAwareFetch } from "../../utils/proxyFetch.js";
import { ANTHROPIC_API_VERSION, CLAUDE_CLI_VERSION } from "../../providers/shared.js";
import { U, parseResetTime } from "./shared.js";

// Claude API config (urls from registry, apiVersion is header logic kept here)
const CLAUDE_CONFIG = {
  oauthUsageUrl: U("claude").oauthUrl,
  usageUrl: U("claude").orgUrl,
  settingsUrl: U("claude").settingsUrl,
  profileUrl: U("claude").profileUrl,
  resetUrl: U("claude").resetUrl,
  apiVersion: ANTHROPIC_API_VERSION,
  // Reset grants are gated by surface: only "(external, cli)" UA is eligible
  userAgent: `claude-cli/${CLAUDE_CLI_VERSION} (external, cli)`,
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

function cacheFresh(accessToken, result) {
  const fresh = { ...result, fetchedAt: Date.now() };
  lastGood.set(accessToken, fresh);
  usageCache.set(accessToken, { result: fresh, expiresAt: Date.now() + USAGE_CACHE_TTL_MS });
  return fresh;
}

export async function getClaudeUsage(accessToken, proxyOptions = null, options = {}) {
  const force = options?.force === true;
  // Bounded callers own their request; do not join an unrelated unbounded dashboard poll.
  // They do share settled fresh readings, else every routed chat request re-polls Anthropic (429s).
  if (options.signal) {
    const hit = !force && accessToken ? usageCache.get(accessToken) : null;
    if (hit?.result?.quotas && !hit.result.stale && hit.expiresAt > Date.now()) return hit.result;
    const result = await fetchClaudeUsageRaw(accessToken, proxyOptions, options.signal);
    return accessToken && result?.quotas ? cacheFresh(accessToken, result) : result;
  }

  // Serve in-flight or fresh cached result (skip on manual force)
  if (!force && accessToken) {
    const hit = usageCache.get(accessToken);
    if (hit?.promise) return hit.promise;
    if (hit && hit.expiresAt > Date.now()) return hit.result;
  }

  const promise = (async () => {
    const result = await fetchClaudeUsageRaw(accessToken, proxyOptions);
    if (!accessToken) return result;
    if (result?.quotas) return cacheFresh(accessToken, result);
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
    // cedar_ember=1 adds the "limit reset" grant block (same flag Claude Code sends)
    const oauthResponse = await proxyAwareFetch(`${CLAUDE_CONFIG.oauthUsageUrl}?cedar_ember=1`, {
      method: "GET",
      signal,
      headers: {
        "Authorization": `Bearer ${accessToken}`,
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": CLAUDE_CONFIG.apiVersion,
        "User-Agent": CLAUDE_CONFIG.userAgent,
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
      // Model-scoped weekly limits (e.g. Fable) arrive in limits[], not as
      // seven_day_* keys: { kind: "weekly_scoped", percent, resets_at,
      // scope: { model: { display_name: "Fable" } } }. No limits entry means
      // the account has no such window — omit the row, never fabricate one.
      if (Array.isArray(data.limits)) {
        for (const limit of data.limits) {
          if (limit?.kind !== "weekly_scoped") continue;
          const modelName = String(limit?.scope?.model?.display_name || "").trim().toLowerCase();
          if (!modelName || typeof limit.percent !== "number") continue;
          quotas[`weekly ${modelName} (7d)`] = createQuotaObject({
            utilization: Math.max(0, Math.min(100, limit.percent)),
            resets_at: limit.resets_at,
          });
        }
      }

      return {
        plan: "Claude Code",
        extraUsage: data.extra_usage ?? null,
        resetCredits: parseClaudeResetGrants(data.cedar_ember),
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

// Free "limit reset" grants (Anthropic program id "cedar_ember").
// Shape: { eligible, next_grant_id, grants: [{ id, resets_left, ends_at, paused, clears }] }
export function parseClaudeResetGrants(block) {
  if (!block?.eligible || !Array.isArray(block.grants)) return null;
  const grants = block.grants.filter((g) => g?.id && !g.paused && Number(g.resets_left) > 0);
  const next = grants.find((g) => g.id === block.next_grant_id) || grants[0] || null;
  return {
    availableCount: grants.reduce((sum, g) => sum + Number(g.resets_left), 0),
    nextGrantId: next?.id || null,
    expiresAt: next?.ends_at || null,
    clears: next?.clears || [],
    cooldownUntil: block.cooldown_until || null,
    weeklyResetsAt: block.weekly_resets_at || null,
    grants: block.grants.filter((g) => g?.id).map((g) => ({
      id: g.id,
      label: g.label || "",
      resetsLeft: Number(g.resets_left) || 0,
      resetsTotal: Number(g.resets_total) || 0,
      startsAt: g.starts_at || null,
      endsAt: g.ends_at || null,
      clears: Array.isArray(g.clears) ? g.clears : [],
      paused: g.paused === true,
      usableNow: g.usable_now === true,
      useRequiresLimit: g.use_requires_limit !== false,
    })),
  };
}

// Spend one reset grant: refills the limits listed in grant.clears. Irreversible.
export async function consumeClaudeResetGrant(accessToken, grantId, proxyOptions = null) {
  if (!accessToken) throw new Error("No Claude access token available. Please re-authorize the connection.");
  if (!/^[a-z0-9_-]{1,40}$/.test(grantId || "")) throw new Error("Invalid reset grant id.");

  const headers = {
    "Authorization": `Bearer ${accessToken}`,
    "anthropic-beta": "oauth-2025-04-20",
    "anthropic-version": CLAUDE_CONFIG.apiVersion,
    "User-Agent": CLAUDE_CONFIG.userAgent,
    "Content-Type": "application/json",
  };

  const profileRes = await proxyAwareFetch(CLAUDE_CONFIG.profileUrl, { method: "GET", headers }, proxyOptions);
  const profile = await profileRes.json().catch(() => null);
  const orgId = profile?.organization?.uuid;
  if (!profileRes.ok || !orgId) throw new Error(`Cannot resolve Claude organization (${profileRes.status}).`);

  const res = await proxyAwareFetch(CLAUDE_CONFIG.resetUrl.replace("{org_id}", orgId), {
    method: "POST",
    headers,
    body: JSON.stringify({ program: "cedar_ember", grant_id: grantId, request_id: crypto.randomUUID() }),
  }, proxyOptions);
  const data = await res.json().catch(() => null);

  usageCache.delete(accessToken); // next read must show refilled limits
  return {
    ok: res.ok && data?.result === "reset",
    status: res.status,
    result: data?.result || null,
    reason: data?.reason || null,
    resetsLeft: data?.resets_left ?? null,
    message: data?.error?.message || null,
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
