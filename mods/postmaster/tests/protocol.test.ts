import { describe, expect, test } from "claude-code/testing";
import { isOutboxEntry as isEntry, isOutboxResult as isResult, isHeartbeat, isEntryId, isOutboxClaim } from "../hooks/protocol";

const entry = {
  id: "0123456789abcdef0123456789abcdef",
  session_id: "recipient",
  socket: "/test-sockets/recipient.sock",
  text: "Message text\n\nwith another line.",
  sha256: "a".repeat(64),
  deadline_ms: 10000,
};
const result = {
  id: entry.id,
  outcome: "delivered",
  detail: "",
  sha256: entry.sha256,
  claimed_ms: 9000,
  finished_ms: 9100,
};
const heartbeat = { session_id: "postmaster-session", at_ms: 9000, version: "0.1.0" };

describe("postmaster protocol validators", () => {
  test("entry accepts the specified fields and preserves message text", () => {
    const value: unknown = JSON.parse(JSON.stringify(entry));
    expect(isEntry(value)).toBe(true);
    if (!isEntry(value)) throw new Error("Valid entry rejected");
    expect(value.text).toBe(entry.text);
    expect(isEntry({ ...entry, text: "" })).toBe(true);
    expect(isEntry({ ...entry, deadline_ms: 1 })).toBe(true);
  });

  test("entry rejects a missing field or a field of the wrong type", () => {
    for (const [name, value] of Object.entries(entry)) {
      const missing: Record<string, unknown> = { ...entry };
      delete missing[name];
      expect(isEntry(missing)).toBe(false);
      expect(isEntry({ ...entry, [name]: typeof value === "number" ? "10000" : 17 })).toBe(false);
    }
  });

  test("entry id must contain exactly 32 hex characters", () => {
    expect(isEntryId(entry.id)).toBe(true);
    for (const id of ["", "a".repeat(31), "a".repeat(33), "g".repeat(32), "../" + "a".repeat(29)]) {
      expect(isEntry({ ...entry, id })).toBe(false);
      expect(isEntryId(id)).toBe(false);
    }
  });

  test("entry ids accept uppercase and mixed hex case with exactly 32 characters", () => {
    for (const id of ["ABCDEF0123456789ABCDEF0123456789", "aBcDeF0123456789AbCdEf0123456789"]) {
      expect(isEntryId(id)).toBe(true);
      expect(isEntry({ ...entry, id })).toBe(true);
    }
    for (const id of ["A".repeat(31), "A".repeat(33), "G" + "A".repeat(31)]) {
      expect(isEntryId(id)).toBe(false);
      expect(isEntry({ ...entry, id })).toBe(false);
    }
  });

  test("entry rejects nonobjects and nonfinite deadlines", () => {
    for (const value of [null, undefined, true, 42, "entry", [], [entry]]) expect(isEntry(value)).toBe(false);
    for (const deadline_ms of [NaN, Infinity, -Infinity]) expect(isEntry({ ...entry, deadline_ms })).toBe(false);
  });

  test("result accepts each specified delivery outcome", () => {
    for (const outcome of ["delivered", "failed", "expired", "withdrawn", "abandoned"]) {
      expect(isResult({ ...result, outcome })).toBe(true);
    }
  });

  test("result rejects missing fields and wrong field types", () => {
    for (const [name, value] of Object.entries(result)) {
      const missing: Record<string, unknown> = { ...result };
      delete missing[name];
      expect(isResult(missing)).toBe(false);
      expect(isResult({ ...result, [name]: typeof value === "number" ? "9000" : 17 })).toBe(false);
    }
    for (const outcome of ["sent", "claimed-no-result", "", "DELIVERED"]) expect(isResult({ ...result, outcome })).toBe(false);
  });

  test("result rejects nonobjects and nonfinite timestamps", () => {
    for (const value of [null, undefined, false, 42, "result", [], [result]]) expect(isResult(value)).toBe(false);
    for (const value of [NaN, Infinity, -Infinity]) {
      expect(isResult({ ...result, claimed_ms: value })).toBe(false);
      expect(isResult({ ...result, finished_ms: value })).toBe(false);
    }
  });

  test("heartbeat accepts session identity time and version", () => {
    expect(isHeartbeat(heartbeat)).toBe(true);
  });

  test("heartbeat rejects missing fields and wrong field types", () => {
    for (const [name, value] of Object.entries(heartbeat)) {
      const missing: Record<string, unknown> = { ...heartbeat };
      delete missing[name];
      expect(isHeartbeat(missing)).toBe(false);
      expect(isHeartbeat({ ...heartbeat, [name]: typeof value === "number" ? "9000" : 17 })).toBe(false);
    }
  });

  test("heartbeat rejects nonobjects and nonfinite timestamps", () => {
    for (const value of [null, undefined, false, 42, "heartbeat", [], [heartbeat]]) expect(isHeartbeat(value)).toBe(false);
    for (const at_ms of [NaN, Infinity, -Infinity]) expect(isHeartbeat({ ...heartbeat, at_ms })).toBe(false);
  });

  test("claim records require an entry id a timestamp and a worker run", () => {
    const claim = { id: entry.id, claimed_ms: 9000, run: "worker-9000" };
    expect(isOutboxClaim(claim)).toBe(true);
    for (const [name, value] of Object.entries(claim)) {
      const missing: Record<string, unknown> = { ...claim };
      delete missing[name];
      expect(isOutboxClaim(missing)).toBe(false);
      expect(isOutboxClaim({ ...claim, [name]: typeof value === "number" ? "9000" : 17 })).toBe(false);
    }
    for (const value of [null, undefined, [], "claim"]) expect(isOutboxClaim(value)).toBe(false);
    for (const claimed_ms of [NaN, Infinity, -Infinity]) expect(isOutboxClaim({ ...claim, claimed_ms })).toBe(false);
  });
});
