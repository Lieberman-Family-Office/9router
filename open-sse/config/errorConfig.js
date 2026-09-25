// OpenAI-compatible error types mapping (client-facing)
export const ERROR_TYPES = {
  400: { type: "invalid_request_error", code: "bad_request" },
  401: { type: "authentication_error", code: "invalid_api_key" },
  402: { type: "billing_error", code: "payment_required" },
  403: { type: "permission_error", code: "insufficient_quota" },
  404: { type: "invalid_request_error", code: "model_not_found" },
  406: { type: "invalid_request_error", code: "model_not_supported" },
  429: { type: "rate_limit_error", code: "rate_limit_exceeded" },
  500: { type: "server_error", code: "internal_server_error" },
  502: { type: "server_error", code: "bad_gateway" },
  503: { type: "server_error", code: "service_unavailable" },
  504: { type: "server_error", code: "gateway_timeout" }
};

// Default error messages per status code (client-facing)
export const DEFAULT_ERROR_MESSAGES = {
  400: "Bad request",
  401: "Invalid API key provided",
  402: "Payment required",
  403: "You exceeded your current quota",
  404: "Model not found",
  406: "Model not supported",
  429: "Rate limit exceeded",
  500: "Internal server error",
  502: "Bad gateway - upstream provider error",
  503: "Service temporarily unavailable",
  504: "Gateway timeout"
};

// Exponential backoff config for rate limits
export const BACKOFF_CONFIG = {
  base: 2000,
  max: 5 * 60 * 1000,
  maxLevel: 15
};

// Default cooldown for transient/unknown errors
export const TRANSIENT_COOLDOWN_MS = 30 * 1000;

// Hard cap for provider-reported rate limit cooldown (e.g. codex resets_at can be 5-6h)
export const MAX_RATE_LIMIT_COOLDOWN_MS = 30 * 60 * 1000;

// Cooldown durations (ms)
const COOLDOWN = {
  long: 2 * 60 * 1000,
  short: 5 * 1000,
};

// USAGE_LIMIT_LOCK_12H=true turns on the two 12 h usage-limit locks. Off by default:
// the production bundle patch that meant to add them never reached the request path,
// so production has never enforced them. Read per call, like ENABLE_REQUEST_LOGS.
export const usageLimitLock12hEnabled = () => process.env.USAGE_LIMIT_LOCK_12H === "true";

const USAGE_LIMIT_RULES_12H = [
  // Must precede "rate limit" (a substring of the first).
  // Source: production's ~/.9router/apply-usage-limit-lock-patch.sh (cooldownMs:432e5).
  { text: "user provided api key rate limit exceeded", cooldownMs: 12 * 60 * 60 * 1000 },
  { text: "usage limit",              cooldownMs: 12 * 60 * 60 * 1000 },
];

// Off: reproduce what production does today. "user provided api key rate limit exceeded"
// falls to the generic "rate limit" backoff, which needs no rule. A 400/422 "usage limit"
// was unmatched there, so it got the transient 30 s lock and failed over; without this
// rule the terminal-400 status rule would stop it instead.
const USAGE_LIMIT_RULES_OFF = [
  { text: "usage limit", statuses: [400, 422], cooldownMs: TRANSIENT_COOLDOWN_MS },
];

/**
 * Unified error classification rules.
 * Checked top-to-bottom: text rules first (by order), then status rules.
 * Each rule: { text?, statuses?, status?, cooldownMs?, backoff?, noFallback? }
 *   - text: substring match (case-insensitive) on error message
 *   - statuses: optional; a text rule applies only to these HTTP statuses
 *   - status: HTTP status code match
 *   - cooldownMs: fixed cooldown duration
 *   - backoff: true = use exponential backoff (rate limit)
 *   - noFallback: true = terminal request error; return it, no lock, no retry
 */
export const errorRules = () => [
  // --- Text-based rules (checked first, order = priority) ---
  { text: "no credentials",           cooldownMs: COOLDOWN.long },
  { text: "request not allowed",      cooldownMs: COOLDOWN.short },
  { text: "improperly formed request", cooldownMs: COOLDOWN.long },
  ...(usageLimitLock12hEnabled() ? USAGE_LIMIT_RULES_12H : USAGE_LIMIT_RULES_OFF),
  { text: "rate limit",               backoff: true },
  { text: "too many requests",        backoff: true },
  { text: "quota exceeded",           backoff: true },
  { text: "capacity",                 backoff: true },
  { text: "overloaded",               backoff: true },
  // Account-level 400s: must still fail over to another account.
  // Strings from public provider docs (Anthropic billing, Gemini API key), not measured here.
  { text: "credit balance is too low", cooldownMs: COOLDOWN.long },
  { text: "api key not valid",        cooldownMs: COOLDOWN.long },

  // --- Status-based rules (fallback when text doesn't match) ---
  { status: 400, noFallback: true },
  { status: 401, cooldownMs: COOLDOWN.long },
  { status: 402, cooldownMs: COOLDOWN.long },
  { status: 403, cooldownMs: COOLDOWN.long },
  { status: 404, cooldownMs: COOLDOWN.long },
  { status: 422, noFallback: true },
  { status: 429, backoff: true },
];

// Backward compat: COOLDOWN_MS object (used by index.js re-export)
export const COOLDOWN_MS = {
  unauthorized: COOLDOWN.long,
  paymentRequired: COOLDOWN.long,
  notFound: COOLDOWN.long,
  transient: TRANSIENT_COOLDOWN_MS,
  requestNotAllowed: COOLDOWN.short,
};
