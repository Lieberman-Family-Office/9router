/**
 * Mid-turn steering state machine + configuration_update continuation tests.
 */
import { describe, expect, it } from "vitest";

import {
  SteerConnectionState,
  STEER_FAIL,
  MAX_PENDING_STEERS,
  validateSteerEvent,
} from "../../open-sse/handlers/responsesWs/steerState.js";
import {
  buildSteerContinuationCreate,
  assertContinuationPreservesRequestEffort,
  configurationUpdateForEffort,
  snapshotRequestReasoning,
} from "../../open-sse/handlers/responsesWs/continuation.js";
import { modelSupportsSteering, isGpt6Family, normalizeModelId } from "../../open-sse/handlers/responsesWs/models.js";

describe("responses mid-turn steering", () => {
  it("GPT-6 family detection includes astra/sol/cx variants", () => {
    expect(isGpt6Family("gpt-6-astra")).toBe(true);
    expect(isGpt6Family("gpt-6-sol")).toBe(true);
    expect(isGpt6Family("cx/gpt-6-astra")).toBe(true);
    expect(isGpt6Family("cx/gpt-6-sol-high")).toBe(true);
    expect(modelSupportsSteering("gpt-6-astra")).toBe(true);
    expect(modelSupportsSteering("gpt-5.6")).toBe(false);
    expect(modelSupportsSteering("gpt-5.5")).toBe(false);
    expect(normalizeModelId("cx/gpt-6-astra-high")).toBe("gpt-6-astra");
  });

  it("validateSteerEvent rejects unknown fields and empty input", () => {
    expect(validateSteerEvent({ type: "response.steer", previous_response_id: "resp_1", input: "ok" }).ok).toBe(true);
    expect(
      validateSteerEvent({ type: "response.steer", previous_response_id: "resp_1", input: "x", model: "gpt-6" }).ok,
    ).toBe(false);
    expect(validateSteerEvent({ type: "response.steer", previous_response_id: "resp_1", input: "" }).code).toBe(STEER_FAIL.INVALID_INPUT);
    expect(validateSteerEvent({ type: "response.steer", previous_response_id: "", input: "x" }).code).toBe(STEER_FAIL.INVALID_INPUT);
  });

  it("accept → pending → continuation state machine", () => {
    const st = new SteerConnectionState();
    st.registerResponse("resp_1", {
      model: "gpt-6-astra",
      createBody: { model: "gpt-6-astra", reasoning: { effort: "medium" }, input: "plan" },
    });

    const accepted = st.handleSteer({
      type: "response.steer",
      previous_response_id: "resp_1",
      input: "Keep scope small",
    });
    expect(accepted.events[0].type).toBe("response.steer.accepted");
    expect(accepted.events[0].steer.id.startsWith("steer_")).toBe(true);

    const pending = st.onResponseTerminal("resp_1", {
      requiredInput: [{ type: "function_call_output", call_id: "call_1", name: "tool" }],
    });
    expect(pending.events[0].type).toBe("response.steer.pending");
    expect(pending.continuationSteer).toBeNull();
    expect(pending.events[0].reason).toBe("waiting_for_required_input");

    st.clearPendingInput("resp_1");
    st.pending[0].status = "queued";
    const cont = st.onResponseTerminal("resp_1", {});
    expect(cont.continuationSteer).toBeTruthy();
    expect(cont.continuationSteer.input).toBe("Keep scope small");
  });

  it("invalid_input and response_not_found and too_many_pending_steers", () => {
    const st = new SteerConnectionState({ maxPending: 2 });
    st.registerResponse("resp_1", { model: "gpt-6-astra", createBody: { model: "gpt-6-astra" } });

    const bad = st.handleSteer({ type: "response.steer", previous_response_id: "resp_1", input: "x", foo: 1 });
    expect(bad.events[0].error.code).toBe(STEER_FAIL.INVALID_INPUT);

    const missing = st.handleSteer({ type: "response.steer", previous_response_id: "resp_missing", input: "x" });
    expect(missing.events[0].error.code).toBe(STEER_FAIL.RESPONSE_NOT_FOUND);

    expect(st.handleSteer({ type: "response.steer", previous_response_id: "resp_1", input: "a" }).events[0].type).toBe("response.steer.accepted");
    expect(st.handleSteer({ type: "response.steer", previous_response_id: "resp_1", input: "b" }).events[0].type).toBe("response.steer.accepted");
    const tooMany = st.handleSteer({ type: "response.steer", previous_response_id: "resp_1", input: "c" });
    expect(tooMany.events[0].error.code).toBe(STEER_FAIL.TOO_MANY_PENDING);
    expect(MAX_PENDING_STEERS).toBeGreaterThanOrEqual(2);
  });

  it("steering_not_supported for non-GPT-6 models", () => {
    const st = new SteerConnectionState();
    st.registerResponse("resp_1", { model: "gpt-5.6", createBody: { model: "gpt-5.6" } });
    const r = st.handleSteer({ type: "response.steer", previous_response_id: "resp_1", input: "x" });
    expect(r.events[0].error.code).toBe(STEER_FAIL.STEERING_NOT_SUPPORTED);
  });

  it("continuation after steer does NOT rewrite top-level reasoning.effort when effort changes via configuration_update", () => {
    const originalCreate = {
      model: "gpt-6-astra",
      reasoning: { effort: "medium", summary: "auto" },
      instructions: "You are a planner.",
      input: "Draft a plan",
      store: false,
    };

    const continuation = buildSteerContinuationCreate({
      originalCreate,
      previousResponseId: "resp_1",
      steerInput: "Now dig deeper on risks.",
      effortUpdate: "high",
    });

    expect(continuation.reasoning.effort).toBe("medium");
    expect(originalCreate.reasoning.effort).toBe("medium");
    expect(continuation.reasoning_effort).toBeUndefined();

    const update = continuation.input.find((it) => it.type === "configuration_update");
    expect(update).toBeTruthy();
    expect(update.reasoning.effort).toBe("high");

    const user = continuation.input.find((it) => it.type === "message" || it.role === "user");
    expect(user).toBeTruthy();

    assertContinuationPreservesRequestEffort(continuation, originalCreate, "high");
  });

  it("Responses wire uses reasoning.effort object; never leaves reasoning_effort on continuation", () => {
    const originalCreate = {
      model: "gpt-6-sol",
      reasoning_effort: "low",
      input: "hi",
    };
    const snap = snapshotRequestReasoning(originalCreate);
    expect(snap.effort).toBe("low");

    const continuation = buildSteerContinuationCreate({
      originalCreate,
      previousResponseId: "resp_9",
      steerInput: "more",
      effortUpdate: "xhigh",
    });
    expect(continuation.reasoning).toEqual({ effort: "low" });
    expect("reasoning_effort" in continuation).toBe(false);
    expect(configurationUpdateForEffort("xhigh").type).toBe("configuration_update");
    expect(continuation.input[0].type).toBe("configuration_update");
    expect(continuation.input[0].reasoning.effort).toBe("xhigh");
  });
});
