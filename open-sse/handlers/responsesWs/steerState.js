/**
 * Connection-scoped mid-turn steering state machine.
 *
 * Events (client → server): response.steer
 * Events (server → client): response.steer.accepted | .pending | .failed
 *
 * Queued steers are connection-local (not persisted across disconnect).
 */

import { randomBytes } from "node:crypto";
import { modelSupportsSteering } from "./models.js";

export const MAX_PENDING_STEERS = 5;

export const STEER_FAIL = Object.freeze({
  INVALID_INPUT: "invalid_input",
  STEERING_NOT_SUPPORTED: "steering_not_supported",
  RESPONSE_NOT_FOUND: "response_not_found",
  TOO_MANY_PENDING: "too_many_pending_steers",
});

function newSteerId() {
  return `steer_${randomBytes(16).toString("hex")}`;
}

/**
 * Validate response.steer payload per OpenAI docs:
 * only type, previous_response_id, and input.
 * @param {object} event
 * @returns {{ ok: true, input: any } | { ok: false, code: string, message: string }}
 */
export function validateSteerEvent(event) {
  if (!event || typeof event !== "object" || Array.isArray(event)) {
    return { ok: false, code: STEER_FAIL.INVALID_INPUT, message: "Steer event must be an object." };
  }
  if (event.type !== "response.steer") {
    return { ok: false, code: STEER_FAIL.INVALID_INPUT, message: "Expected type response.steer." };
  }
  const allowed = new Set(["type", "previous_response_id", "input", "stream_id"]);
  for (const key of Object.keys(event)) {
    if (!allowed.has(key)) {
      return {
        ok: false,
        code: STEER_FAIL.INVALID_INPUT,
        message: `Unsupported steer field: ${key}`,
      };
    }
  }
  const prev = event.previous_response_id;
  if (typeof prev !== "string" || !prev.trim()) {
    return {
      ok: false,
      code: STEER_FAIL.INVALID_INPUT,
      message: "previous_response_id is required.",
    };
  }
  const input = event.input;
  if (typeof input === "string") {
    if (!input.trim()) {
      return { ok: false, code: STEER_FAIL.INVALID_INPUT, message: "input must be nonempty." };
    }
    return { ok: true, input };
  }
  if (Array.isArray(input)) {
    if (input.length === 0) {
      return { ok: false, code: STEER_FAIL.INVALID_INPUT, message: "input array must be nonempty." };
    }
    return { ok: true, input };
  }
  return {
    ok: false,
    code: STEER_FAIL.INVALID_INPUT,
    message: "input must be a string or nonempty array of user messages.",
  };
}

/**
 * @typedef {object} PendingSteer
 * @property {string} id
 * @property {string} previous_response_id
 * @property {any} input
 * @property {"queued"|"pending_input"|"applied"|"failed"} status
 * @property {any[]|null} required_input
 */

export class SteerConnectionState {
  /**
   * @param {object} [opts]
   * @param {number} [opts.maxPending]
   */
  constructor(opts = {}) {
    this.maxPending = opts.maxPending ?? MAX_PENDING_STEERS;
    /** @type {Map<string, { model: string, provider?: string, createBody: object, status: string, outputItems: any[] }>} */
    this.responses = new Map();
    /** @type {PendingSteer[]} */
    this.pending = [];
    this.seq = 0;
    this.closed = false;
  }

  nextSeq() {
    this.seq += 1;
    return this.seq;
  }

  /**
   * Register a response started on this connection.
   * @param {string} responseId
   * @param {{ model: string, provider?: string, createBody: object }} meta
   */
  registerResponse(responseId, meta) {
    this.responses.set(responseId, {
      model: meta.model,
      provider: meta.provider,
      createBody: meta.createBody,
      status: "in_progress",
      outputItems: [],
    });
  }

  markResponseStatus(responseId, status) {
    const r = this.responses.get(responseId);
    if (r) r.status = status;
  }

  appendOutputItem(responseId, item) {
    const r = this.responses.get(responseId);
    if (r) r.outputItems.push(item);
  }

  pendingCount() {
    return this.pending.filter((s) => s.status === "queued" || s.status === "pending_input").length;
  }

  /**
   * Handle client response.steer.
   * @param {object} event
   * @returns {{ events: object[] }}
   */
  handleSteer(event) {
    const events = [];
    const seq = () => this.nextSeq();

    if (this.closed) {
      events.push(failedEvent(seq(), null, event?.previous_response_id, STEER_FAIL.RESPONSE_NOT_FOUND, "Connection closed."));
      return { events };
    }

    const validated = validateSteerEvent(event);
    if (!validated.ok) {
      events.push(
        failedEvent(seq(), null, event?.previous_response_id, validated.code, validated.message)
      );
      return { events };
    }

    const prevId = String(event.previous_response_id);
    const target = this.responses.get(prevId);
    if (!target) {
      events.push(
        failedEvent(seq(), null, prevId, STEER_FAIL.RESPONSE_NOT_FOUND, `Response ${prevId} not found on this connection.`)
      );
      return { events };
    }

    if (!modelSupportsSteering(target.model)) {
      const id = newSteerId();
      events.push(
        failedEvent(
          seq(),
          id,
          prevId,
          STEER_FAIL.STEERING_NOT_SUPPORTED,
          `Model ${target.model} does not support mid-turn steering (GPT-6 family required).`
        )
      );
      return { events };
    }

    if (this.pendingCount() >= this.maxPending) {
      const id = newSteerId();
      events.push(
        failedEvent(
          seq(),
          id,
          prevId,
          STEER_FAIL.TOO_MANY_PENDING,
          `Too many pending steers (max ${this.maxPending}).`
        )
      );
      return { events };
    }

    const steer = {
      id: newSteerId(),
      previous_response_id: prevId,
      input: validated.input,
      status: "queued",
      required_input: null,
    };
    this.pending.push(steer);
    events.push({
      type: "response.steer.accepted",
      sequence_number: seq(),
      steer: { id: steer.id, previous_response_id: prevId },
    });
    return { events };
  }

  /**
   * After a response finishes, if it left required tool/approval input and steers are queued,
   * emit pending; otherwise return the next queued steer for auto-continuation.
   * @param {string} responseId
   * @param {{ requiredInput?: any[] }} [opts]
   * @returns {{ events: object[], continuationSteer: PendingSteer|null }}
   */
  onResponseTerminal(responseId, opts = {}) {
    const events = [];
    const required = Array.isArray(opts.requiredInput) ? opts.requiredInput : null;
    const queued = this.pending.filter(
      (s) => s.previous_response_id === responseId && s.status === "queued"
    );
    if (!queued.length) return { events, continuationSteer: null };

    if (required && required.length) {
      for (const steer of queued) {
        steer.status = "pending_input";
        steer.required_input = required;
        events.push({
          type: "response.steer.pending",
          sequence_number: this.nextSeq(),
          steer: { id: steer.id, previous_response_id: steer.previous_response_id },
          reason: "waiting_for_required_input",
          required_input: required,
        });
      }
      return { events, continuationSteer: null };
    }

    const next = queued[0];
    next.status = "applied";
    return { events, continuationSteer: next };
  }

  /**
   * Mark steers waiting on required_input as ready after client continuation create.
   * @param {string} previousResponseId
   */
  clearPendingInput(previousResponseId) {
    for (const s of this.pending) {
      if (s.previous_response_id === previousResponseId && s.status === "pending_input") {
        s.status = "applied";
        s.required_input = null;
      }
    }
  }

  close() {
    this.closed = true;
    this.pending = [];
    this.responses.clear();
  }
}

function failedEvent(sequence_number, steerId, previous_response_id, code, message) {
  return {
    type: "response.steer.failed",
    sequence_number,
    steer: steerId
      ? { id: steerId, previous_response_id: previous_response_id || null }
      : previous_response_id
        ? { id: null, previous_response_id }
        : null,
    error: { code, message },
  };
}
