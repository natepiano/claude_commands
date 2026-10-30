import { mock } from "claude-code/testing";
import type { On, UiPane, EngineInterface } from "claude-code";
import { register } from "../hooks/register";
import type { PaneState } from "../types";
import type { BoardData, BoardPhase, BoardTime } from "../types/board";

export const NOW = Date.UTC(2026, 9, 16, 18, 0);
export const BOARD = "/test-state/unit-board/show.json";
export const RUN = "/history/runs/current.jsonl";
export const DISCOVERY = "/history/runs";
export const REGISTRY = "/test-home/.local/state/notifier";
export const CWD = "/test-worktree";
export const SESSION = "showrunner-session-id";
export const time = (minutes: number, text: string): BoardTime => ({ at: NOW / 1000 + minutes * 60, text });

export function phase(id: string, status: BoardPhase["status"]): BoardPhase {
  return { id, title: `Delivery ${id}`, status, took: status === "done" ? 600 : null,
    fix_done: 0, fix_running: 0, findings: { blocker: 0, minor: 0, nit: 0 },
    eta: { first: null, trail: [], finished: null, delta_minutes: null },
    start: null, finish: null, gate: { kind: "none" } };
}

export function fixture(): BoardData {
  const done = phase("1", "done");
  done.fix_done = 1;
  done.eta = { first: time(-30, "17:30"),
    trail: [[time(-30, "17:30"), time(-60, "17:00")]],
    finished: time(-40, "17:20"), delta_minutes: -10 };
  const open = phase("2", "open");
  open.start = time(-60, "17:00");
  return { production: "show", as_of: time(0, "18:00"), zone: "America/New_York",
    utc_offset_minutes: -240, sources: [RUN, DISCOVERY], units: [{
      key: "alpha", plan: "plan.md", state: "running", phase: { kind: "plan", id: "2", of: 3, title: "Delivery 2" },
      phases: [done, open, phase("3", "todo")], finish: { kind: "predicted", at: time(42, "18:42") },
      stage: "Repair round 2", summary: "Finishing the retry path",
      eta: { at: time(30, "18:30"), first: time(-45, "17:15"),
        trail: [[time(-45, "17:15"), time(-90, "16:30")],
          [time(-15, "17:45"), time(-75, "16:45")], [time(30, "18:30"), time(-60, "17:00")]],
        moved: 2, delta_minutes: 75, schedule_end: time(30, "18:30"), fix_done: 1, fix_running: 1 },
      steps: [["completed", "Build retry path", time(-50, "17:10"), 600],
        ["running", "Check retry path", time(-10, "17:50"), 600]],
      workers: [{ slot: "1", role: "fix", state: "running", model: "codex/fixture",
        doing: "Fixing the retry case" }],
    }] };
}

export function hostEngine(on: On, sessionId = () => SESSION) {
  const panes = new Map<string, UiPane>();
  const opened: string[] = [];
  const closed: string[] = [];
  const forbidden: string[] = [];
  on("session.id", () => ({ value: sessionId() }));
  on("session.start", (_$, event) => ({ cwd: event.cwd }));
  on("turn.complete", (_$, event) => ({ text: event.answer }));
  on("command.register", (_$, event) => ({ value: { command: event.name } }));
  on("ui.panes", () => ({ value: [...panes.values()] }));
  on("ui.open", (_$, event) => {
    opened.push(event.id);
    panes.set(event.id, { id: event.id, title: event.title ?? event.id,
      isShown: true, isFocused: true, isPlaced: true });
    return { value: { isPlaced: true } };
  });
  on("ui.close", (_$, event) => {
    closed.push(event.id);
    panes.delete(event.id);
    return { value: undefined };
  });
  on("ui.status", () => { forbidden.push("status"); return { value: undefined }; });
  on("ui.toast", () => { forbidden.push("toast"); return { value: undefined }; });
  return { opened, closed, forbidden };
}

export function engineFiles(on: On) {
  let sessionId = SESSION;
  const host = hostEngine(on, () => sessionId);
  const clock = mock.clock(on, { now: NOW });
  mock.env(on, { HOME: "/test-home", XDG_STATE_HOME: "/test-state" });
  let boards = [fixture()];
  let failure = "";
  const files = new Map([[BOARD, JSON.stringify(boards[0])]]);
  const mtimes = new Map([[BOARD, NOW], [RUN, NOW - 1000], [DISCOVERY, NOW - 1000], [REGISTRY, NOW - 1000]]);
  const reads: string[] = [];
  const stats: string[] = [];
  const processes: string[][] = [];
  on("fs.exists", (_$, e, next) => { stats.push(e.path); return next(e); });
  on("fs.list", (_$, e, next) => { stats.push(e.path); return next(e); });
  on("fs.write", (_$, e, next) => { stats.push(e.path); return next(e); });
  on("session.cwd", () => ({ value: CWD }));
  on("process.run", (_$, e) => {
    processes.push([...e.argv]);
    if (failure) {
      const stderr = failure;
      failure = "";
      return { value: { exitCode: 1, stdout: "", stderr, isStdoutTruncated: false, isStderrTruncated: false } };
    }
    const paths = boards.map((board, index) => {
      const path = index === 0 ? BOARD : `/test-state/unit-board/${board.production}.json`;
      files.set(path, JSON.stringify(board));
      mtimes.set(path, clock.now());
      return path;
    });
    return { value: { exitCode: 0, stdout: JSON.stringify({ files: paths, watch: [REGISTRY] }), stderr: "",
      isStdoutTruncated: false, isStderrTruncated: false } };
  });
  on("fs.read", (_$, e) => {
    reads.push(e.path);
    const value = files.get(e.path);
    if (value === undefined) throw new Error(`Missing fixture ${e.path}`);
    return { value };
  });
  on("fs.stat", (_$, e) => {
    stats.push(e.path);
    const mtimeMs = mtimes.get(e.path);
    if (mtimeMs === undefined) throw new Error(`Missing fixture ${e.path}`);
    return { value: { kind: e.path === DISCOVERY ? "dir" : "file", size: 0, mtimeMs, isLink: false } };
  });
  return { ...host, clock, reads, stats, processes, files, mtimes,
    replace: (value: BoardData) => { boards = [value]; },
    replaceBoards: (values: BoardData[]) => { boards = values; },
    failNext: (reason: string) => { failure = reason; },
    setSession: (value: string) => { sessionId = value; },
    clear: () => { reads.length = 0; stats.length = 0; processes.length = 0; },
  };
}

/** Read only displayed text, excluding props, callback ids, and style metadata. */
export function drawnText(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(drawnText).join(" ");
  if (typeof value !== "object" || value === null) return "";
  const record = value as Record<string, unknown>;
  const props = typeof record.props === "object" && record.props !== null
    ? record.props as Record<string, unknown> : {};
  return [record.text, record.children, record.label, props.label].map(drawnText).filter(Boolean).join(" ");
}

export function element(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? value as Record<string, unknown> : {};
}

/** Inspect layout rows, inherited styles and host clipping props in one traversal. */
export function inspectDrawing(tree: unknown) {
  type Props = Record<string, unknown>;
  const rows: Props[] = [];
  const textProps: Props[] = [];
  const nodes: Props[] = [];
  const spans: { text: string; color: string; bold: boolean }[] = [];
  const visit = (value: unknown, color = "default", bold = false): string[] => {
    if (typeof value === "string" || typeof value === "number") {
      spans.push({ text: String(value), color, bold });
      return String(value).split("\n");
    }
    if (Array.isArray(value)) return value.flatMap(child => visit(child, color, bold));
    if (typeof value !== "object" || value === null) return [];
    const node = element(value);
    const props = element(node.props);
    nodes.push(node);
    if (node.type === "Text") textProps.push(props);
    if (node.type === "Box" && props.flexDirection === "row") rows.push(props);
    const children = Array.isArray(node.children) ? node.children : node.children === undefined ? [] : [node.children];
    const parts = children.map(child => visit(child,
      typeof props.color === "string" ? props.color : color,
      typeof props.bold === "boolean" ? props.bold : bold)).filter(lines => lines.length);
    if (node.type === "Text") return [parts.flat().join("")];
    if (node.type !== "Box") return parts.flat();
    const top = Number(props.marginTop ?? props.marginY ?? props.margin ?? 0)
      + Number(props.paddingTop ?? props.paddingY ?? props.padding ?? 0);
    const bottom = Number(props.marginBottom ?? props.marginY ?? props.margin ?? 0)
      + Number(props.paddingBottom ?? props.paddingY ?? props.padding ?? 0);
    const lines = props.flexDirection === "column"
      ? parts.flatMap((part, index) => [...Array<string>(index ? Number(props.rowGap ?? props.gap ?? 0) : 0).fill(""), ...part])
      : parts.length || props.flexDirection === "row"
        ? [parts.flat().join(" ".repeat(Number(props.columnGap ?? props.gap ?? 0)))] : [];
    const indent = " ".repeat(Number(props.paddingLeft ?? props.paddingX ?? props.padding ?? 0));
    return [...Array<string>(top).fill(""), ...lines.map(line => indent + line), ...Array<string>(bottom).fill("")];
  };
  const lines = visit(tree);
  return { lines, spans, rows, textProps, nodes };
}

// Keep existing accounts assertions expressed in their original projections.
export const rowLines = (tree: unknown) => inspectDrawing(tree).lines;
export const styledText = (tree: unknown) => inspectDrawing(tree).spans;
export const elements = (tree: unknown) => inspectDrawing(tree).nodes;

/** Minimal host for operation events the testing Engine cannot raise. */
export function lifecycleHarness() {
  type LifecycleHook = (services: EngineInterface, event: unknown,
    next: (event: unknown) => Promise<unknown>) => unknown;
  const hooks = new Map<string, { filter: Record<string, unknown>; hook: LifecycleHook }[]>();
  const collect = (pattern: string, ...arguments_: unknown[]) => {
    const filter = arguments_.length > 1 ? arguments_[0] as Record<string, unknown> : {};
    const chain = hooks.get(pattern) ?? [];
    chain.push({ filter, hook: arguments_.at(-1) as LifecycleHook });
    hooks.set(pattern, chain);
    return { catch: () => {} };
  };
  register(collect as On, {});
  let panes: Record<string, PaneState<BoardData>> = {};
  let cancelled = 0;
  let forwarded = 0;
  const calls: string[] = [];
  const timers: { active: boolean; tick: () => void }[] = [];
  let release = () => {};
  let signal = () => {};
  let pending: Promise<void> = Promise.resolve();
  let sourceMtime = NOW;
  const services = {
    state: {
      get: async () => ({ value: panes }),
      set: async (_key: unknown, value: Record<string, PaneState<BoardData>>) => { calls.push("state"); panes = value; },
    },
    ui: { open: async () => ({ isPlaced: true }) },
    session: { cwd: async () => CWD, id: async () => SESSION },
    env: { get: async (name: string) => name === "HOME" ? "/test-home" : undefined },
    clock: {
      now: async () => NOW,
      every: (_ms: number, tick: () => void) => {
        const timer = { active: true, tick };
        timers.push(timer);
        return { cancel: () => { timer.active = false; cancelled++; } };
      },
    },
    fs: {
      read: async () => { calls.push("read"); return JSON.stringify(fixture()); },
      stat: async (path: string) => { calls.push("stat"); return { mtimeMs: path === RUN ? sourceMtime : NOW }; },
    },
    process: { run: async () => {
      calls.push("process");
      signal();
      await pending;
      return { exitCode: 0, stdout: JSON.stringify({ files: [BOARD], watch: [REGISTRY] }), stderr: "" };
    } },
  };
  // The registered hooks use only these services. Keep the partial host at this
  // boundary rather than inventing implementations for unused engine APIs.
  const engine = services as unknown as EngineInterface;
  return {
    pauseGenerator: () => {
      const started = new Promise<void>(resolve => { signal = resolve; });
      pending = new Promise<void>(resolve => { release = resolve; });
      return { started, finish: () => release() };
    },
    changeSource: () => { sourceMtime = NOW + 1; },
    calls, panes: () => panes, cancelledTimers: () => cancelled,
    forwardedCloses: () => forwarded, clearCalls: () => { calls.length = 0; },
    dispatch: async (event: string, input: unknown) => {
      const chain = hooks.get(event);
      if (!chain) throw new Error(`No registered ${event} hook`);
      const dispatchAt = async (index: number, value: unknown): Promise<unknown> => {
        const entry = chain[index];
        if (!entry) {
          if (event === "ui.close") forwarded++;
          return value;
        }
        const fields = value as Record<string, unknown>;
        if (!Object.entries(entry.filter).every(([key, expected]) => fields[key] === expected)) {
          return dispatchAt(index + 1, value);
        }
        return entry.hook(engine, value, nextValue => dispatchAt(index + 1, nextValue));
      };
      return dispatchAt(0, input);
    },
    advanceMinute: async () => {
      for (let tick = 0; tick < 6; tick++) {
        for (const timer of timers) if (timer.active) timer.tick();
      }
    },
  };
}
