/**
 * Detect CLI tool identity from request headers/body.
 * Used to determine if a request can be passed through losslessly.
 */

// Map of CLI tool identifiers to provider IDs they are "native" to
const NATIVE_PAIRS = {
  "claude": ["claude", "anthropic"],
  "gemini-cli": ["gemini-cli"],
  "antigravity": ["antigravity"],
  "codex": ["codex"],
};

// Native passthrough is only safe when the body is already in the provider's
// wire format. Claude Code UA + OpenAI-shaped tools (e.g. combo→Mac hop, or a
// client that speaks OpenAI against a Claude account) must still translate —
// otherwise Anthropic rejects `tools[].type: "function"` with HTTP 400.
const NATIVE_SOURCE_FORMATS = {
  claude: ["claude"],
  "gemini-cli": ["gemini", "gemini-cli"],
  antigravity: ["antigravity"],
  codex: ["openai-responses", "codex"],
};

/**
 * Detect which CLI tool is making the request.
 * Returns one of: "claude" | "gemini-cli" | "antigravity" | "codex" | null
 * @param {object} headers - Lowercase header key/value object
 * @param {object} body    - Parsed request body
 */
export function detectClientTool(headers = {}, body = {}) {
  const ua = (headers["user-agent"] || "").toLowerCase();
  const xApp = (headers["x-app"] || "").toLowerCase();
  const openaiIntent = (headers["openai-intent"] || "").toLowerCase();
  const initiator = (headers["x-initiator"] || headers["X-Initiator"] || "").toLowerCase();
  const originator = (headers["originator"] || "").toLowerCase();

  // Antigravity: detected via body field (not header)
  if (body.userAgent === "antigravity") return "antigravity";

  // GitHub Copilot / OAI compatible extension using Copilot chat headers
  if (ua.includes("githubcopilotchat") || openaiIntent === "conversation-panel" || initiator === "user") {
    return "github-copilot";
  }

  // Claude Code / Claude CLI
  if (ua.includes("claude-cli") || ua.includes("claude-code") || xApp === "cli") return "claude";

  // Gemini CLI
  if (ua.includes("gemini-cli")) return "gemini-cli";

  // Codex CLI/Desktop — codex-tui is the current Rust CLI, codex-cli/codex_cli_rs legacy;
  // Codex Desktop identifies via UA "Codex Desktop" or originator "codex_work_desktop"
  if (ua.includes("codex-tui") || ua.includes("codex-cli") || ua.includes("codex_cli_rs") ||
      ua.includes("codex desktop") || originator.startsWith("codex_")) return "codex";

  // DeepSeek TUI
  if (ua.includes("deepseek-tui")) return "deepseek-tui";

  return null;
}

/**
 * Check if this CLI tool + provider pair should be passed through losslessly.
 * @param {string|null} clientTool - Result of detectClientTool()
 * @param {string} provider        - Provider ID (e.g. "claude", "gemini-cli")
 * @param {string|null} [sourceFormat] - Detected body format; when provided,
 *   must match the client tool's native wire format (see NATIVE_SOURCE_FORMATS)
 */
export function isNativePassthrough(clientTool, provider, sourceFormat = null) {
  if (!clientTool) return false;
  const nativeProviders = NATIVE_PAIRS[clientTool];
  if (!nativeProviders) return false;
  // Support anthropic-compatible-* variants
  const normalizedProvider = provider.startsWith("anthropic-compatible")
    ? "anthropic"
    : provider;
  if (!nativeProviders.includes(normalizedProvider)) return false;
  if (sourceFormat == null) return true;
  const allowed = NATIVE_SOURCE_FORMATS[clientTool];
  if (!allowed) return false;
  return allowed.includes(sourceFormat);
}
