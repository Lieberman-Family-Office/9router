/**
 * Responses WebSocket session: routes client events and bridges response.create
 * to the local HTTP `/v1/responses` SSE path. Implements mid-turn steering state
 * on the connection; auto-continuations preserve request-level reasoning.effort
 * and use configuration_update for mid-conversation effort changes.
 */

import { SteerConnectionState, STEER_FAIL } from "./steerState.js";
import { buildSteerContinuationCreate } from "./continuation.js";
import { modelSupportsSteering, steerUpstreamMode } from "./models.js";
import { encodeTextFrame, encodeCloseFrame, WsFrameReader } from "./wsFrames.js";

/**
 * @param {object} opts
 * @param {import("node:net").Socket} opts.socket
 * @param {import("node:http").IncomingMessage} opts.req
 * @param {(path: string, headers: object, body: object) => Promise<Response>} opts.fetchLocalResponses
 * @param {(model: string) => { provider: string }} [opts.resolveRoute]
 */
export function createResponsesWsSession({ socket, req, fetchLocalResponses, resolveRoute }) {
  const state = new SteerConnectionState();
  const reader = new WsFrameReader();
  let closed = false;
  /** @type {AbortController|null} */
  let activeAbort = null;
  /** Last create body for continuations */
  let lastCreate = null;
  let lastStreamId = null;
  let lastProvider = null;

  const send = (obj) => {
    if (closed || socket.destroyed) return;
    try {
      socket.write(encodeTextFrame(JSON.stringify(obj)));
    } catch {
      /* ignore */
    }
  };

  const close = (code = 1000, reason = "") => {
    if (closed) return;
    closed = true;
    state.close();
    try {
      socket.write(encodeCloseFrame(code, reason));
    } catch {
      /* ignore */
    }
    socket.end();
  };

  const handleClientEvent = async (event) => {
    if (!event || typeof event !== "object") {
      send({
        type: "error",
        status: 400,
        error: { type: "invalid_request_error", message: "Expected JSON object event." },
      });
      return;
    }
    const type = event.type;
    if (type === "response.steer") {
      // Provider/route gate: Codex ChatGPT has no upstream WS steer — still allow
      // local continuation for GPT-6; emit steering_not_supported only when model
      // or explicit unsupported mode blocks it.
      const target = state.responses.get(event.previous_response_id);
      if (target && !modelSupportsSteering(target.model)) {
        send({
          type: "response.steer.failed",
          sequence_number: state.nextSeq(),
          steer: { id: null, previous_response_id: event.previous_response_id },
          error: {
            code: STEER_FAIL.STEERING_NOT_SUPPORTED,
            message: `Model ${target.model} does not support mid-turn steering.`,
          },
        });
        return;
      }
      if (lastProvider && steerUpstreamMode(lastProvider) === "unsupported") {
        send({
          type: "response.steer.failed",
          sequence_number: state.nextSeq(),
          steer: { id: null, previous_response_id: event.previous_response_id },
          error: {
            code: STEER_FAIL.STEERING_NOT_SUPPORTED,
            message: `Provider ${lastProvider} does not support mid-turn steering.`,
          },
        });
        return;
      }
      const { events } = state.handleSteer(event);
      for (const ev of events) send(ev);
      return;
    }

    if (type === "response.create") {
      await runCreate(event);
      return;
    }

    send({
      type: "error",
      status: 400,
      stream_id: event.stream_id,
      error: {
        type: "invalid_request_error",
        message: `Unsupported event type: ${type}`,
      },
    });
  };

  const runCreate = async (event, { isSteerContinuation = false } = {}) => {
    const createBody = { ...event };
    delete createBody.type;
    const model = createBody.model || lastCreate?.model;
    if (!model) {
      send({
        type: "error",
        status: 400,
        error: { type: "invalid_request_error", message: "model is required." },
      });
      return;
    }
    createBody.model = model;
    createBody.stream = true;

    // Responses wire: prefer reasoning.effort object; drop Chat Completions twin.
    if (createBody.reasoning_effort != null && !createBody.reasoning) {
      createBody.reasoning = { effort: createBody.reasoning_effort };
    }
    delete createBody.reasoning_effort;

    const route = resolveRoute ? resolveRoute(model) : { provider: "codex" };
    lastProvider = route.provider;
    lastCreate = { ...createBody };
    lastStreamId = event.stream_id || lastStreamId;

    if (activeAbort) activeAbort.abort();
    activeAbort = new AbortController();

    const headers = {};
    const auth = req.headers.authorization;
    if (auth) headers.authorization = auth;
    if (req.headers["x-api-key"]) headers["x-api-key"] = req.headers["x-api-key"];
    headers.accept = "text/event-stream";
    headers["content-type"] = "application/json";

    let responseId = null;
    let sawFunctionCall = false;
    /** @type {any[]} */
    const requiredInput = [];

    try {
      const res = await fetchLocalResponses("/v1/responses", headers, createBody);
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        send({
          type: "error",
          status: res.status,
          stream_id: event.stream_id,
          error: {
            type: "api_error",
            message: text.slice(0, 500) || `HTTP ${res.status}`,
          },
        });
        return;
      }

      const readerBody = res.body?.getReader?.();
      if (!readerBody) {
        // Node fetch may give a web stream or node stream — normalize via text/SSE parse
        const text = await res.text();
        await consumeSseText(text);
        return;
      }

      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await readerBody.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n");
        buf = parts.pop() || "";
        for (const line of parts) {
          await onSseLine(line);
        }
      }
      if (buf) await onSseLine(buf);
    } catch (err) {
      if (err?.name === "AbortError") return;
      send({
        type: "error",
        status: 502,
        error: { type: "api_error", message: String(err?.message || err) },
      });
    }

    async function onSseLine(line) {
      const trimmed = line.trimEnd();
      if (!trimmed.startsWith("data:")) return;
      const data = trimmed.slice(5).trim();
      if (!data || data === "[DONE]") return;
      let ev;
      try {
        ev = JSON.parse(data);
      } catch {
        return;
      }
      // Attach stream_id for WS multiplexing clients
      if (event.stream_id && ev && typeof ev === "object" && !ev.stream_id) {
        ev = { ...ev, stream_id: event.stream_id };
      }
      if (!ev.sequence_number) ev.sequence_number = state.nextSeq();

      if (ev.type === "response.created" && ev.response?.id) {
        responseId = ev.response.id;
        state.registerResponse(responseId, {
          model,
          provider: lastProvider,
          createBody: lastCreate,
        });
      }
      if (ev.type === "response.output_item.done" && ev.item) {
        if (responseId) state.appendOutputItem(responseId, ev.item);
        if (ev.item.type === "function_call" || ev.item.type === "custom_tool_call") {
          sawFunctionCall = true;
          requiredInput.push({
            type: ev.item.type === "custom_tool_call" ? "custom_tool_call_output" : "function_call_output",
            call_id: ev.item.call_id,
            name: ev.item.name,
          });
        }
        if (ev.item.type === "mcp_approval_request") {
          sawFunctionCall = true;
          requiredInput.push({
            type: "mcp_approval_response",
            approval_request_id: ev.item.id,
          });
        }
      }

      send(ev);

      const terminal = ["response.completed", "response.incomplete", "response.failed"].includes(ev.type);
      if (terminal && responseId) {
        state.markResponseStatus(responseId, ev.type.replace("response.", ""));
        const reqIn = sawFunctionCall ? requiredInput : null;
        const { events: pendingEv, continuationSteer } = state.onResponseTerminal(responseId, {
          requiredInput: reqIn || undefined,
        });
        for (const pe of pendingEv) send(pe);
        if (continuationSteer && !isSteerContinuation) {
          // Auto-continuation: inherit settings; do not rewrite request-level effort.
          const cont = buildSteerContinuationCreate({
            originalCreate: lastCreate,
            previousResponseId: responseId,
            steerInput: continuationSteer.input,
            streamId: lastStreamId,
          });
          // If original ended without steered incomplete, still continue.
          await runCreate(cont, { isSteerContinuation: true });
        }
      }
    }

    async function consumeSseText(text) {
      for (const line of String(text).split(/\r?\n/)) {
        await onSseLine(line);
      }
    }
  };

  socket.on("data", (chunk) => {
    const frames = reader.push(chunk);
    for (const frame of frames) {
      if (frame.opcode === 0x8 || frame.oversized) {
        close(1000);
        return;
      }
      if (frame.opcode === 0x9) {
        // ping → pong
        const pong = Buffer.from([0x8a, frame.payload.length, ...frame.payload]);
        socket.write(Buffer.from([0x8a, frame.payload.length]));
        if (frame.payload.length) socket.write(frame.payload);
        void pong;
        continue;
      }
      if (frame.opcode !== 0x1 && frame.opcode !== 0x0) continue;
      let event;
      try {
        event = JSON.parse(frame.payload.toString("utf8"));
      } catch {
        send({
          type: "error",
          status: 400,
          error: { type: "invalid_request_error", message: "Invalid JSON." },
        });
        continue;
      }
      Promise.resolve(handleClientEvent(event)).catch((err) => {
        send({
          type: "error",
          status: 500,
          error: { type: "server_error", message: String(err?.message || err) },
        });
      });
    }
  });

  socket.on("error", () => close(1011, "socket error"));
  socket.on("close", () => {
    closed = true;
    state.close();
    if (activeAbort) activeAbort.abort();
  });

  return { close, state, send };
}
