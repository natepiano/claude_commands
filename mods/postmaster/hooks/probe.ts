import type { EngineInterface, SessionSendAddress, SessionStartInput, SessionReceiveInput } from "claude-code";

export type ProbeCalls = {
  env: Pick<EngineInterface["env"], "get">;
  fs: Pick<EngineInterface["fs"], "write" | "exists" | "list"> & {
    read(path: string): ReturnType<EngineInterface["fs"]["read"]>;
  };
  session: Pick<EngineInterface["session"], "id" | "send">;
};
type EnvironmentSetting =
  | { status: "found"; value: string; source: string }
  | { status: "missing"; reason: string };
type EnvironmentAnswer =
  | { status: "returned"; answer: unknown }
  | { status: "threw"; reason: string };
type EnvironmentRead = { setting: EnvironmentSetting; engine_answer: EnvironmentAnswer };
type SessionIdentity =
  | { status: "found"; value: string; source: string }
  | { status: "missing"; reason: string };
type WorkerIdentity =
  | { status: "found"; value: number; source: string }
  | { status: "missing"; reason: string };
type ConsumptionState =
  | { status: "unarmed" }
  | { status: "armed"; request_id: string };
type ReceiveDisposition =
  | { status: "pass" }
  | { status: "consumed"; reason: string };
type ProbeLifecycle =
  | { status: "waiting" }
  | { status: "running"; probe: Probe };
type ProbeRequest = {
  id: string;
  step: string;
  to_session_id: string;
  to_socket: string;
  text: string;
  count: number;
};
type StepResult = { ok: boolean; detail: unknown };
type ReportLine = StepResult & { run: number; id: string; step: string; started_ms: number; ended_ms: number };
type Probe = {
  api: ProbeCalls;
  directory: string;
  consumption: ConsumptionState;
  session: SessionIdentity;
  seen: Set<string>;
  invalid_requests: Set<string>;
  pending: ReportLine[];
  flushing: boolean;
  journals: Map<string, string>;
  writes: Promise<void>;
  polling: boolean;
  last_poll_ms: number;
  max_gap_ms: number;
  last_completed_ms: number;
  max_completed_gap_ms: number;
  completed_count: number;
  tick_ms: number;
  poll_count: number;
};

const run = Date.now();
let lifecycle: ProbeLifecycle = { status: "waiting" };

function member(owner: unknown, name: string): unknown {
  if ((typeof owner !== "object" || owner === null) && typeof owner !== "function") return undefined;
  try { return (owner as Record<string, unknown>)[name]; }
  catch { return undefined; }
}

function errorText(error: unknown): string {
  const message = member(error, "message");
  return typeof message === "string" ? message : String(error);
}

function json(value: unknown): string {
  return JSON.stringify(value, (_key: string, item: unknown) => {
    if (item === undefined) return { status: "undefined" };
    if (typeof item === "bigint") return { bigint: String(item) };
    return item;
  }) ?? "null";
}

async function environment(api: ProbeCalls, name: string): Promise<EnvironmentRead> {
  let engine_answer: EnvironmentAnswer;
  try {
    const answer = await api.env.get(name);
    engine_answer = { status: "returned", answer };
    if (typeof answer === "string" && answer !== "") {
      return { setting: { status: "found", value: answer, source: `$.env.get(${name})` }, engine_answer };
    }
  } catch (error) { engine_answer = { status: "threw", reason: errorText(error) }; }
  const value = member(member(member(globalThis, "process"), "env"), name);
  const setting: EnvironmentSetting = typeof value === "string" && value !== ""
    ? { status: "found", value, source: `globalThis.process.env.${name}` }
    : { status: "missing", reason: `${name} has no string value` };
  return { setting, engine_answer };
}

async function sessionIdentity(api: ProbeCalls, event: SessionStartInput): Promise<SessionIdentity> {
  let reason = "$.session.id() has no nonempty string value";
  try {
    const value = await api.session.id();
    if (typeof value === "string" && value !== "") return { status: "found", value, source: "$.session.id()" };
  } catch (error) { reason = errorText(error); }
  const value = member(event, "session_id");
  return typeof value === "string" && value !== ""
    ? { status: "found", value, source: "session.start.session_id" }
    : { status: "missing", reason };
}

async function read(probe: Probe, name: string): Promise<string> {
  const value = await probe.api.fs.read(`${probe.directory}/${name}`);
  if (typeof value !== "string") throw new Error("File read did not return text");
  return value;
}

async function write(probe: Probe, name: string, text: string): Promise<void> {
  await probe.api.fs.write(`${probe.directory}/${name}`, text);
}

function append(probe: Probe, name: string, value: unknown): Promise<void> {
  const writing = probe.writes.then(async () => {
    let prior = probe.journals.get(name) ?? "";
    try {
      if (await probe.api.fs.exists(`${probe.directory}/${name}`)) prior = await read(probe, name);
    } catch { /* Keep this worker's earlier lines when journal reads fail. */ }
    const text = prior + `${json(value)}\n`;
    await write(probe, name, text);
    probe.journals.set(name, text);
  });
  probe.writes = writing.catch(() => {});
  return writing;
}

function queue(probe: Probe, id: string, step: string, result: StepResult, started_ms = Date.now()): void {
  probe.pending.push({ run, id, step, ...result, started_ms, ended_ms: Date.now() });
}

async function flush(probe: Probe): Promise<void> {
  if (probe.flushing) return;
  probe.flushing = true;
  try {
    while (probe.pending.length > 0) {
      const line = probe.pending[0];
      if (!line) break;
      await append(probe, "report.jsonl", line);
      probe.pending.shift();
    }
  } finally { probe.flushing = false; }
}

function request(value: unknown): ProbeRequest {
  for (const name of ["id", "step", "to_session_id", "to_socket", "text"]) {
    if (typeof member(value, name) !== "string") throw new Error(`request.${name} must be a string`);
  }
  const count = member(value, "count");
  if (typeof count !== "number" || !Number.isSafeInteger(count) || count < 0) {
    throw new Error("request.count must be a nonnegative integer");
  }
  return value as ProbeRequest;
}

async function send(probe: Probe, to: SessionSendAddress, text: string): Promise<StepResult> {
  const started_ms = Date.now();
  try {
    const answer = await probe.api.session.send({ to, text });
    return { ok: true, detail: { disposition: "returned", answer, started_ms, ended_ms: Date.now() } };
  } catch (error) {
    return { ok: false, detail: { disposition: "threw", error: errorText(error), started_ms, ended_ms: Date.now() } };
  }
}

async function files(probe: Probe): Promise<StepResult> {
  const path = `${probe.directory}/file-source.txt`;
  const operations: unknown[] = [];
  const first = "postmaster file trial\n";
  const second = "postmaster file trial, second write\n";
  let ok = true;
  const unchecked = (_answer: unknown): void => {};
  const equals = (expected: string | boolean) => (answer: unknown): void => {
    if (answer !== expected) throw new Error("File result differs from expected value");
  };
  const operationsToRun: [string, string, () => Promise<unknown>, (answer: unknown) => void][] = [
    ["write", "write", () => probe.api.fs.write(path, first), unchecked],
    ["list", "list", () => probe.api.fs.list(probe.directory), unchecked],
    ["read", "read", () => probe.api.fs.read(path), equals(first)],
    ["exists", "exists", () => probe.api.fs.exists(path), equals(true)],
    ["write-again", "write", () => probe.api.fs.write(path, second), unchecked],
    ["read-again", "read", () => probe.api.fs.read(path), equals(second)],
  ];
  for (const [operation, method, invoke, check] of operationsToRun) {
    const started_ms = Date.now();
    try {
      const answer = await invoke();
      check(answer);
      operations.push({ operation, api: `$.fs.${method}`, ok: true, answer, started_ms, ended_ms: Date.now() });
    } catch (error) {
      ok = false;
      operations.push({ operation, api: `$.fs.${method}`, ok: false, error: errorText(error), started_ms, ended_ms: Date.now() });
    }
  }
  return { ok, detail: { backend: "$.fs", path, first_content: first, final_content: second, operations,
    rename: { status: "unavailable", reason: "$.fs has no rename API on Claude Code 2.1.296" },
    remove: { status: "unavailable", reason: "$.fs has no remove API on Claude Code 2.1.296" } } };
}

function metrics(probe: Probe, now = Date.now()) {
  return { poll_count: probe.poll_count, max_gap_ms: probe.max_gap_ms, completed_count: probe.completed_count,
    max_completed_gap_ms: Math.max(probe.max_completed_gap_ms, now - probe.last_completed_ms) };
}

async function step(probe: Probe, item: ProbeRequest): Promise<StepResult> {
  switch (item.step) {
    case "send-by-id":
    case "send-dead":
      return send(probe, { sessionId: item.to_session_id }, item.text);
    case "send-by-address":
      return send(probe, `uds:${item.to_socket}`, item.text);
    case "send-self":
      return probe.session.status === "found"
        ? send(probe, { sessionId: probe.session.value }, item.text)
        : { ok: false, detail: probe.session };
    case "burst": {
      const sends: StepResult[] = [];
      for (let index = 0; index < item.count; index++) sends.push(await send(probe, { sessionId: item.to_session_id }, item.text));
      return { ok: sends.every(result => result.ok), detail: { count: item.count, sends } };
    }
    case "files": return files(probe);
    case "consume":
      if (item.id === "") return { ok: false, detail: "Consumption requires a nonempty request id" };
      probe.consumption = { status: "armed", request_id: item.id };
      return { ok: true, detail: { status: "armed", request_id: item.id, match: "session.receive.text contains request id", scope: "next matching message" } };
    case "timer":
      return { ok: true, detail: { mechanism: "globalThis.setInterval", interval_ms: 500, tick_interval_ms: 60000, ...metrics(probe) } };
    default: return { ok: false, detail: `Unknown step: ${item.step}` };
  }
}

function requestError(probe: Probe, text: string, value: unknown, error: unknown): void {
  if (probe.invalid_requests.has(text)) return;
  probe.invalid_requests.add(text);
  const id = member(value, "id");
  queue(probe, typeof id === "string" ? id : "request", "request-error", { ok: false, detail: errorText(error) });
}

async function checkRequest(probe: Probe): Promise<void> {
  try {
    if (!await probe.api.fs.exists(`${probe.directory}/request.json`)) return;
    const text = await read(probe, "request.json");
    let value: unknown;
    let item: ProbeRequest;
    try { value = JSON.parse(text) as unknown; item = request(value); }
    catch (error) { requestError(probe, text, value, error); return; }
    if (probe.seen.has(item.id)) return;
    probe.seen.add(item.id);
    const started_ms = Date.now();
    let result: StepResult;
    try { result = await step(probe, item); }
    catch (error) { result = { ok: false, detail: errorText(error) }; }
    queue(probe, item.id, item.step, result, started_ms);
  } catch (error) { requestError(probe, `read-error:${errorText(error)}`, null, error); }
  finally {
    const now = Date.now();
    probe.max_completed_gap_ms = Math.max(probe.max_completed_gap_ms, now - probe.last_completed_ms);
    probe.last_completed_ms = now;
    probe.completed_count++;
  }
}

async function poll(probe: Probe): Promise<void> {
  const now = Date.now();
  probe.max_gap_ms = Math.max(probe.max_gap_ms, now - probe.last_poll_ms);
  probe.last_poll_ms = now;
  probe.poll_count++;
  if (now - probe.tick_ms >= 60000) {
    queue(probe, "tick", "tick", { ok: true, detail: { mechanism: "globalThis.setInterval", ...metrics(probe, now) } }, now);
    probe.tick_ms = now;
    probe.max_gap_ms = 0;
    probe.max_completed_gap_ms = 0;
  }
  await flush(probe);
  if (probe.polling) return;
  probe.polling = true;
  try { await checkRequest(probe); }
  finally { probe.polling = false; }
  await flush(probe);
}

export async function start(api: ProbeCalls, event: SessionStartInput): Promise<void> {
  if (lifecycle.status === "running") {
    const probe = lifecycle.probe;
    queue(probe, "start", "start", { ok: true, detail: { session_id: probe.session.status === "found" ? probe.session.value : null, already_running: true } });
    await flush(probe);
    return;
  }
  const home = await environment(api, "HOME");
  const xdg = await environment(api, "XDG_STATE_HOME");
  const state = xdg.setting.status === "found" ? xdg.setting.value
    : home.setting.status === "found" ? `${home.setting.value}/.local/state` : "";
  if (!state.startsWith("/")) throw new Error("No absolute home or XDG_STATE_HOME is available");
  const session = await sessionIdentity(api, event);
  const process = member(globalThis, "process");
  const value = member(process, "pid");
  const pid: WorkerIdentity = typeof value === "number" && Number.isSafeInteger(value) && value > 0
    ? { status: "found", value, source: "globalThis.process.pid (hooks worker)" }
    : { status: "missing", reason: "globalThis.process.pid is unavailable; no mod call gives the session pid" };
  const now = Date.now();
  const probe: Probe = { api, directory: `${state}/message/postmaster/probe`, consumption: { status: "unarmed" }, session,
    seen: new Set(), invalid_requests: new Set(), pending: [], flushing: false, journals: new Map(), writes: Promise.resolve(), polling: false,
    last_poll_ms: now, max_gap_ms: 0, last_completed_ms: now, max_completed_gap_ms: 0, completed_count: 0, tick_ms: now, poll_count: 0 };
  lifecycle = { status: "running", probe };
  try {
    const prior = await read(probe, "report.jsonl");
    probe.journals.set("report.jsonl", prior);
    for (const line of prior.split("\n")) {
      try {
        const record: unknown = JSON.parse(line);
        const id = member(record, "id");
        const step = member(record, "step");
        if (typeof id === "string" && typeof step === "string" && !["start", "tick", "request-error", "hook-error"].includes(step)) probe.seen.add(id);
      } catch { /* Ignore incomplete journal lines. */ }
    }
  } catch { /* A new directory has no earlier report. */ }
  queue(probe, "start", "start", { ok: true, detail: { session_id: session.status === "found" ? session.value : null, already_running: false } });
  try {
    await write(probe, "started.json", json({ session_id: session.status === "found" ? session.value : null, pid: pid.status === "found" ? pid.value : null,
      session_read: session, pid_read: pid, home_read: home.setting, environment_read: { HOME: { ...home.engine_answer, resolved: home.setting }, XDG_STATE_HOME: { ...xdg.engine_answer, resolved: xdg.setting } },
      process_env_available: typeof member(process, "env") === "object" && member(process, "env") !== null, file_api: "$.fs", event }));
  } catch (error) { queue(probe, "hook", "hook-error", { ok: false, detail: errorText(error) }); }
  const interval = member(globalThis, "setInterval");
  if (typeof interval !== "function") {
    queue(probe, "timer", "timer", { ok: false, detail: "globalThis.setInterval is unavailable" });
  } else {
    (interval as (callback: () => void, milliseconds: number) => unknown)(() => { void poll(probe).catch(() => {}); }, 500);
  }
  await flush(probe);
}

export async function receive(event: SessionReceiveInput): Promise<ReceiveDisposition> {
  if (lifecycle.status !== "running") return { status: "pass" };
  const probe = lifecycle.probe;
  const text = member(event, "text");
  const armed = probe.consumption;
  if (armed.status === "armed" && typeof text === "string" && text.includes(armed.request_id)) {
    probe.consumption = { status: "unarmed" };
    const fields = typeof event === "object" && event !== null ? event : { event };
    await append(probe, "received.jsonl", { ...fields, consumed: true, consume_request_id: armed.request_id });
    return { status: "consumed", reason: `Postmaster probe request ${armed.request_id}` };
  }
  await append(probe, "received.jsonl", event);
  return { status: "pass" };
}

export async function failed(event: unknown): Promise<void> {
  if (lifecycle.status !== "running") return;
  const probe = lifecycle.probe;
  queue(probe, "hook", "hook-error", { ok: false, detail: { message: errorText(event), value: event } });
  await flush(probe).catch(() => {});
}
