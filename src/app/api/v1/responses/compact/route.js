import { handleChat } from "@/sse/handlers/chat.js";
import { initTranslators } from "open-sse/translator/index.js";

let initialized = false;

async function ensureInitialized() {
  if (!initialized) {
    await initTranslators();
    initialized = true;
  }
}

export async function OPTIONS() {
  return new Response(null, {
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "*"
    }
  });
}

/**
 * POST /v1/responses/compact - Compact conversation context
 * Reuses the same handleChat pipeline, signals compact via body._compact
 */
export async function POST(request) {
  await ensureInitialized();
  const body = await request.json();
  // OpenAI docs: Multi-agent is incompatible with standalone /responses/compact.
  if (body?.multi_agent?.enabled === true) {
    return Response.json(
      {
        error: {
          message:
            "multi_agent is not supported with /responses/compact; disable multi_agent or use server-side context_management compaction",
          type: "invalid_request_error",
          code: "multi_agent_compact_incompatible",
        },
      },
      { status: 400 },
    );
  }
  body._compact = true;
  const newRequest = new Request(request.url, {
    method: "POST",
    headers: request.headers,
    body: JSON.stringify(body)
  });
  return await handleChat(newRequest);
}
