import { describe, expect, test } from "claude-code/testing";
import { isOutboxEntry as isEntry, isOutboxResult as isResult, isHeartbeat, isEntryId, isOutboxClaim, isDeliveryRouteFile, isForwardLog, isReplyLog, PATHS } from "../hooks/protocol";

const entry = {
  id: "0123456789abcdef0123456789abcdef",
  session_id: "recipient",
  socket: "uds:/test-sockets/recipient.sock",
  sender: "Original sender",
  reply_to_session_id: "original-session",
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
const heartbeat = { session_id: "postmaster-session", at_ms: 9000, version: "0.1.0", run: "9000-test-worker" };

describe("postmaster protocol validators", () => {
  test("entry requires a sender name and permits an unresolved reply destination on the wire", () => {
    expect(isEntry({ ...entry, sender: "" })).toBe(false);
    const missing: Record<string, unknown> = { ...entry };
    delete missing.reply_to_session_id;
    expect(isEntry(missing)).toBe(false);
    expect(isEntry({ ...entry, reply_to_session_id: "" })).toBe(true);
    expect(PATHS.delivered).toBe("delivered");
    expect(PATHS.replies).toBe("replies");
    expect(PATHS.forwards).toBe("forwards");
  });

  test("route files accept open and withdrawn routes with unresolved wire destinations", () => {
    const route = { route: "open", socket: "uds:/test-sockets/recipient.sock", sender: "Original sender",
      reply_to_session_id: "original-session", session_id: "recipient", at_ms: 1000 };
    expect(isDeliveryRouteFile(route)).toBe(true);
    expect(isDeliveryRouteFile({ ...route, route: "withdrawn" })).toBe(true);
    expect(isDeliveryRouteFile({ ...route, reply_to_session_id: "" })).toBe(true);
    for (const [field, value] of Object.entries(route)) {
      const missing: Record<string, unknown> = { ...route };
      delete missing[field];
      expect(isDeliveryRouteFile(missing)).toBe(false);
      expect(isDeliveryRouteFile({ ...route, [field]: typeof value === "number" ? "1000" : 17 })).toBe(false);
    }
    for (const routeState of ["", "delivered", "OPEN"]) expect(isDeliveryRouteFile({ ...route, route: routeState })).toBe(false);
    for (const at_ms of [-1, 0.5, NaN, Infinity]) expect(isDeliveryRouteFile({ ...route, at_ms })).toBe(false);
    for (const value of [null, undefined, [], "route"]) expect(isDeliveryRouteFile(value)).toBe(false);
  });

  test("reply logs require the replying identity original sender body and intended targets", () => {
    const log = { at_ms: 1000, socket: "uds:/test-sockets/recipient.sock", name: "Recipient", session_id: "recipient",
      senders: ["Original sender"], body: "answer", targets: ["original-session"] };
    expect(isReplyLog(log)).toBe(true);
    expect(isReplyLog({ ...log, targets: [] })).toBe(true);
    for (const field of Object.keys(log)) {
      const missing: Record<string, unknown> = { ...log };
      delete missing[field];
      expect(isReplyLog(missing)).toBe(false);
    }
    for (const targets of ["original-session", [17], [""], null]) expect(isReplyLog({ ...log, targets })).toBe(false);
  });

  test("forward logs require a list of confirmed destinations", () => {
    expect(isForwardLog({ forwarded: ["original-session"] })).toBe(true);
    expect(isForwardLog({ forwarded: [] })).toBe(true);
    for (const forwarded of ["original-session", [17], [""], null, undefined]) expect(isForwardLog({ forwarded })).toBe(false);
    for (const value of [null, [], "forwarded"]) expect(isForwardLog(value)).toBe(false);
  });

  test("reply logs require a nonempty list of nonempty sender names", () => {
    const log = { at_ms: 1000, socket: "uds:/test-sockets/recipient.sock", name: "Recipient",
      session_id: "recipient", senders: ["Alice", "Bob"], body: "answer", targets: [] };
    expect(isReplyLog(log)).toBe(true);
    for (const senders of [[], [""], ["Alice", ""], [17], "Alice", null]) {
      expect(isReplyLog({ ...log, senders })).toBe(false);
    }
    const { senders: _senders, ...rest } = log;
    expect(isReplyLog({ ...rest, sender: "Alice, Bob" })).toBe(false);
  });

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

  test("heartbeat requires a nonempty worker identity", () => {
    const { run: _run, ...withoutWorker } = heartbeat;
    expect(isHeartbeat(withoutWorker)).toBe(false);
    expect(isHeartbeat({ ...heartbeat, run: "" })).toBe(false);
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
