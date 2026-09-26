/**
 * Build automatic steering continuations for Responses WebSocket.
 *
 * Cache-preserving rule (OpenAI GPT-6 guidance):
 * - Keep request-level `reasoning.effort` unchanged across the conversation.
 * - Encode mid-conversation effort changes as `configuration_update` input items.
 * - Responses API uses `reasoning: { effort }`; Chat Completions uses top-level `reasoning_effort`.
 */

/**
 * Normalize steer `input` into Responses input items (user messages).
 * @param {string|any[]} input
 * @returns {any[]}
 */
export function normalizeSteerInputItems(input) {
  if (typeof input === "string") {
    return [
      {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: input }],
      },
    ];
  }
  if (!Array.isArray(input)) return [];
  return input.map((item) => {
    if (typeof item === "string") {
      return {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: item }],
      };
    }
    if (item && typeof item === "object") {
      if (item.type === "configuration_update") return { ...item };
      if (item.role || item.type === "message") {
        return {
          type: "message",
          role: item.role || "user",
          content: item.content ?? item.text ?? "",
        };
      }
    }
    return item;
  });
}

/**
 * Build a `configuration_update` item for mid-conversation effort change.
 * Does NOT mutate request-level reasoning.
 * @param {string} effort
 * @returns {{ type: "configuration_update", reasoning: { effort: string } }}
 */
export function configurationUpdateForEffort(effort) {
  return {
    type: "configuration_update",
    reasoning: { effort: String(effort) },
  };
}

/**
 * Snapshot request-level reasoning from an original response.create body.
 * Always returns Responses shape `{ effort }` when present — never Chat `reasoning_effort` alone.
 * @param {object} createBody
 * @returns {{ effort?: string, [k: string]: any }|undefined}
 */
export function snapshotRequestReasoning(createBody) {
  if (!createBody || typeof createBody !== "object") return undefined;
  if (createBody.reasoning && typeof createBody.reasoning === "object") {
    return { ...createBody.reasoning };
  }
  if (createBody.reasoning_effort != null) {
    // Client may have sent Chat Completions field on a Responses create — normalize to object.
    return { effort: createBody.reasoning_effort };
  }
  return undefined;
}

/**
 * Build the auto-continuation `response.create` payload after an accepted steer.
 *
 * CRITICAL: never rewrite top-level `reasoning.effort` / `reasoning_effort` when
 * applying an effort change — put it in `input` as `configuration_update`.
 *
 * @param {object} opts
 * @param {object} opts.originalCreate - original response.create fields (minus type)
 * @param {string} opts.previousResponseId
 * @param {string|any[]} opts.steerInput
 * @param {string} [opts.effortUpdate] - if set, prepend configuration_update; do not change request-level effort
 * @param {any[]} [opts.extraInput] - tool outputs / approvals to include before steer text
 * @param {string} [opts.streamId]
 * @returns {object} response.create event body
 */
export function buildSteerContinuationCreate({
  originalCreate,
  previousResponseId,
  steerInput,
  effortUpdate,
  extraInput = [],
  streamId,
}) {
  const base = { ...(originalCreate || {}) };
  delete base.type;
  delete base.stream; // WS transport — not used
  delete base.background;

  const requestReasoning = snapshotRequestReasoning(base);
  // Preserve baseline effort object; strip Chat Completions twin from Responses wire.
  if (requestReasoning) {
    base.reasoning = { ...requestReasoning };
  }
  delete base.reasoning_effort;

  const input = [];
  if (Array.isArray(extraInput) && extraInput.length) {
    input.push(...extraInput);
  }
  if (effortUpdate != null && String(effortUpdate).length) {
    // Mid-conversation effort: configuration_update only — baseline reasoning untouched.
    input.push(configurationUpdateForEffort(effortUpdate));
  }
  input.push(...normalizeSteerInputItems(steerInput));

  const create = {
    type: "response.create",
    ...base,
    previous_response_id: previousResponseId,
    input,
  };
  if (streamId) create.stream_id = streamId;

  // Final guard: effortUpdate must not have rewritten request-level effort.
  if (effortUpdate != null && requestReasoning?.effort != null) {
    if (create.reasoning?.effort !== requestReasoning.effort) {
      create.reasoning = { ...requestReasoning };
    }
  }
  if ("reasoning_effort" in create) {
    delete create.reasoning_effort;
  }

  return create;
}

/**
 * Assert helper for tests / self-checks.
 * @param {object} continuationCreate
 * @param {object} originalCreate
 * @param {string} [effortUpdate]
 */
export function assertContinuationPreservesRequestEffort(continuationCreate, originalCreate, effortUpdate) {
  const baseline = snapshotRequestReasoning(originalCreate);
  const next = snapshotRequestReasoning(continuationCreate);
  if (baseline?.effort != null && next?.effort !== baseline.effort) {
    throw new Error(
      `continuation rewrote request-level reasoning.effort (${baseline.effort} → ${next?.effort})`
    );
  }
  if (continuationCreate.reasoning_effort != null) {
    throw new Error("Responses continuation must not set top-level reasoning_effort");
  }
  if (effortUpdate != null) {
    const hasUpdate = (continuationCreate.input || []).some(
      (it) => it && it.type === "configuration_update" && it.reasoning?.effort === String(effortUpdate)
    );
    if (!hasUpdate) {
      throw new Error("expected configuration_update for effort change");
    }
  }
}
