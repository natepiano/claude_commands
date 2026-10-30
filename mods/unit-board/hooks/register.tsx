import type { BoardData } from '../types/board';
import type { EngineInterface, Register, Timer } from 'claude-code';
import type { AccountsPaneState, BoardSnapshot, PaneState } from '../types';
import { loadAccounts, loadBoard, refreshIfChanged, resolveUserZone, snapshotPaths } from './data';
import type { AccountsLoader, BoardLoader, RefreshPaths, UserZoneSettings } from './data';
import { BoardView } from './board';
import { UnitView } from './unit';
import { AccountsView } from './accounts';
import type { StoredAccountsStatus } from '../types/accounts';
import { QueueView } from './queue';
import type { QueuePaneState, QueueLines } from '../types/queue';

const PANE = 'unit-board';
const ACCOUNTS = 'accounts-board';
const ACCOUNT_PANES = { plugin: 'unit-board', key: 'accountsPanes' } as const;
const QUEUE = 'build-queue';
const QUEUE_PANES = { plugin: 'unit-board', key: 'queuePanes' } as const;
let queueColumns = 100;
type QueueWidthRefresh = { kind: 'idle' } | { kind: 'requested'; columns: number };
let queueWidthRefresh: QueueWidthRefresh = { kind: 'idle' };
const PANES = { plugin: 'unit-board', key: 'panes' } as const;

type Selection = { kind: 'board' } | { kind: 'unit'; production: string; unit: string };
type OpenPane = { timer: Timer; cwd: string; session: string; busy: boolean; paths: RefreshPaths[]; selection: Selection };

function boardCalls($: EngineInterface): BoardLoader {
  return {
    home: () => $.env.get('HOME'),
    registry: () => $.env.get('NOTIFIER_STATE_DIR'),
    now: () => $.clock.now(),
    run: argv => $.process.run(argv),
    read: path => $.fs.read(path),
  };
}

function viewState(data: BoardSnapshot<BoardData>, selection: Selection, now: number): PaneState<BoardData> {
  if (selection.kind === 'unit' && data.boards.find(board => board.production === selection.production)?.units.some(unit => unit.key === selection.unit)) {
    return { ...selection, data, now };
  }
  return { kind: 'board', data, now };
}

function commandSelection(data: BoardSnapshot<BoardData>, wanted: string): Selection {
  if (!wanted) return { kind: 'board' };
  const board = data.boards.find(board => board.units.some(unit => unit.key === wanted));
  return board ? { kind: 'unit', production: board.production, unit: wanted } : { kind: 'board' };
}

function retainWatch(active: OpenPane, path: RefreshPaths) {
  if (path.kind === 'board') { active.paths.push(path); return; }
  const previous = active.paths.filter(path => path.kind === 'discovery');
  active.paths = [...active.paths.filter(path => path.kind === 'board'), {
    kind: 'discovery', sources: [...new Set([...previous.flatMap(path => path.sources), ...path.sources])],
    loadedAt: Math.min(path.loadedAt, ...previous.map(path => path.loadedAt)),
  }];
}

// Only put writes these entries; the self-contained engine contract cannot import BoardData.
const readPane = async ($: EngineInterface, id: string) =>
  (await $.state.get(PANES)).value?.[id] as PaneState<BoardData> | undefined;
const put = async ($: EngineInterface, id: string, state: PaneState<BoardData>, isOpen: () => boolean) => {
  if (!isOpen()) return;
  const old = (await $.state.get(PANES)).value ?? {};
  if (isOpen()) await $.state.set(PANES, { ...old, [id]: state });
};
const stop = async ($: EngineInterface, opened: Map<string, OpenPane>, id: string) => {
  opened.get(id)?.timer.cancel();
  opened.delete(id);
  const panes = { ...(await $.state.get(PANES)).value };
  delete panes[id];
  await $.state.set(PANES, panes);
};
const tick = async ($: EngineInterface, opened: Map<string, OpenPane>, id: string) => {
  const active = opened.get(id);
  if (!active || active.busy) return;
  const isOpen = () => opened.get(id) === active;
  active.busy = true;
  try {
    const state = await readPane($, id);
    if (!state || !isOpen()) return;
    const regenerated = await refreshIfChanged({ stat: path => $.fs.stat(path) }, active.paths,
      () => loadBoard(boardCalls($), active.cwd, active.session, isOpen, path => retainWatch(active, path)), async data => {
        const now = await $.clock.now() / 1000;
        if (!isOpen()) return;
        active.paths = snapshotPaths(data);
        const fresh = viewState(data, active.selection, now);
        active.selection = fresh.kind === 'unit' ? { kind: 'unit', production: fresh.production, unit: fresh.unit } : { kind: 'board' };
        await put($, id, fresh, isOpen);
      }, isOpen);
    if (regenerated || !isOpen()) return;
    if (state.kind === 'board' || state.kind === 'unit') {
      await put($, id, { ...state, now: await $.clock.now() / 1000 }, isOpen);
    }
  } catch (error) {
    await put($, id, { kind: 'failed', reason: String(error) }, isOpen);
  } finally { active.busy = false; }
};
const startTimer = ($: EngineInterface, opened: Map<string, OpenPane>, id: string, cwd: string, session: string) => {
  const previous = opened.get(id);
  previous?.timer.cancel();
  opened.set(id, { cwd, session, busy: false, paths: previous?.paths ?? [], selection: previous?.selection ?? { kind: 'board' },
    timer: $.clock.every(10_000, () => { void tick($, opened, id); }) });
};
const select = async ($: EngineInterface, opened: Map<string, OpenPane>, id: string, selection: Selection) => {
  const active = opened.get(id);
  if (!active) return;
  const current = await readPane($, id);
  if (current?.kind !== 'board' && current?.kind !== 'unit') return;
  active.selection = selection;
  await put($, id, viewState(current.data, selection, current.now), () => opened.get(id) === active);
};

function accountsCalls($: EngineInterface): AccountsLoader {
  return {
    home: () => $.env.get('HOME'), now: () => $.clock.now(),
    stat: path => $.fs.stat(path), run: argv => $.process.run(argv),
  };
}
function zoneSettings($: EngineInterface): UserZoneSettings {
  return {
    override: () => $.env.get('USER_ZONE'), config: () => $.env.get('USER_ZONE_CONF'),
    home: () => $.env.get('HOME'), read: path => $.fs.read(path),
  };
}
type TablePaneState<Ready> = { kind: 'loading' } | { kind: 'failed'; reason: string } | Ready;
type ClosedTablePane = { kind: 'closed'; generation: number };
type PreparingTablePane = { kind: 'preparing'; generation: number };
type OpenTablePane<Context> = { kind: 'open'; generation: number; timer: Timer; busy: boolean;
  refreshRequested: boolean; paths: RefreshPaths[]; context: Context };
type TablePaneLifecycle<Context> = ClosedTablePane | PreparingTablePane | OpenTablePane<Context>;
interface TablePaneSettings {
  failure: string;
  refresh: 'changed' | 'always';
}
interface TablePaneCalls<Ready, Context> {
  prepare(isOpen: () => boolean): Promise<Context>;
  load(context: Context, isOpen: () => boolean, watch: (paths: RefreshPaths[]) => void): Promise<Ready>;
  advance(state: Ready): Promise<Ready>;
  read(): Promise<TablePaneState<Ready> | undefined>;
  put(state: TablePaneState<Ready>, isOpen: () => boolean): Promise<void>;
  drop(isClosed: () => boolean): Promise<void>;
  open(): Promise<unknown>;
  every(callback: () => void): Timer;
  stat(path: string): Promise<{ mtimeMs: number }>;
}

/** One lifecycle for one-shot table readers; closing invalidates every pending await. */
function tablePane<Ready extends { kind: 'accounts' | 'queue' }, Context>(settings: TablePaneSettings) {
  let lifecycle: TablePaneLifecycle<Context> = { kind: 'closed', generation: 0 };
  let latestGeneration = 0;
  const read = async (calls: TablePaneCalls<Ready, Context>, current: OpenTablePane<Context>) => {
    const isOpen = () => lifecycle === current;
    const fresh = await calls.load(current.context, isOpen, paths => { if (isOpen()) current.paths = paths; });
    if (isOpen()) await calls.put(fresh, isOpen);
  };
  const refresh = async (calls: TablePaneCalls<Ready, Context>, force = false) => {
    const current = lifecycle;
    if (current.kind !== 'open') return;
    if (current.busy) {
      if (force) current.refreshRequested = true;
      return;
    }
    const isOpen = () => lifecycle === current;
    current.busy = true;
    try {
      if (force || settings.refresh === 'always' || !current.paths.length) {
        await read(calls, current);
        return;
      }
      const regenerated = await refreshIfChanged({ stat: path => calls.stat(path) }, current.paths,
        () => calls.load(current.context, isOpen, paths => { if (isOpen()) current.paths = paths; }),
        fresh => calls.put(fresh, isOpen), isOpen);
      if (regenerated || !isOpen()) return;
      const state = await calls.read();
      if (state && state.kind !== 'loading' && state.kind !== 'failed' && isOpen()) {
        const fresh = await calls.advance(state);
        if (isOpen()) await calls.put(fresh, isOpen);
      }
    } catch {
      await calls.put({ kind: 'failed', reason: settings.failure }, isOpen);
    } finally {
      current.busy = false;
      if (isOpen() && current.refreshRequested) {
        current.refreshRequested = false;
        await refresh(calls, true);
      }
    }
  };
  const start = async (calls: TablePaneCalls<Ready, Context>, open: boolean) => {
    const preparing: PreparingTablePane = { kind: 'preparing', generation: ++latestGeneration };
    const paths = lifecycle.kind === 'open' ? lifecycle.paths : [];
    if (lifecycle.kind === 'open') lifecycle.timer.cancel();
    lifecycle = preparing;
    const isCurrent = () => lifecycle === preparing && latestGeneration === preparing.generation;
    const context = await calls.prepare(isCurrent);
    if (!isCurrent()) return false;
    if (open) await calls.open();
    if (!isCurrent()) return false;
    const current: OpenTablePane<Context> = { kind: 'open', generation: preparing.generation,
      context, paths, busy: false, refreshRequested: false,
      timer: calls.every(() => { void refresh(calls); }) };
    lifecycle = current;
    if (open) {
      current.busy = true;
      try { await calls.put({ kind: 'loading' }, () => lifecycle === current); }
      finally { current.busy = false; }
    }
    await refresh(calls, open);
    return lifecycle === current;
  };
  const stop = async (calls: TablePaneCalls<Ready, Context>) => {
    const closed: ClosedTablePane = { kind: 'closed', generation: ++latestGeneration };
    if (lifecycle.kind === 'open') lifecycle.timer.cancel();
    lifecycle = closed;
    await calls.drop(() => lifecycle === closed && latestGeneration === closed.generation);
  };
  return { start, stop, refresh, checkpoint: () => ++latestGeneration,
    resume: async (calls: TablePaneCalls<Ready, Context>, generation: number) => {
      if (generation === latestGeneration) await start(calls, false);
    } };
}

type AccountsReady = Extract<AccountsPaneState<StoredAccountsStatus>, { kind: 'accounts' }>;
type AccountsZone = Awaited<ReturnType<typeof resolveUserZone>>;
function accountsPaneCalls($: EngineInterface): TablePaneCalls<AccountsReady, AccountsZone> {
  return {
    open: () => $.ui.open({ id: ACCOUNTS, title: 'Accounts', closeOnEscape: true }),
    every: callback => $.clock.every(10_000, callback),
    stat: path => $.fs.stat(path),
    prepare: isOpen => resolveUserZone(zoneSettings($), isOpen),
    load: async (zone, isOpen, watch) => {
      if (zone.kind !== 'resolved') throw new Error('Pane closed.');
      const data = await loadAccounts(accountsCalls($), isOpen, watch);
      if (!isOpen()) throw new Error('Pane closed.');
      return { kind: 'accounts', data, zone: zone.name, now: await $.clock.now() / 1000 };
    },
    advance: async state => ({ ...state, now: await $.clock.now() / 1000 }),
    read: async () => (await $.state.get(ACCOUNT_PANES)).value?.[ACCOUNTS] as AccountsPaneState<StoredAccountsStatus> | undefined,
    put: async (state, isOpen) => {
      if (!isOpen()) return;
      const previous = (await $.state.get(ACCOUNT_PANES)).value ?? {};
      if (isOpen()) await $.state.set(ACCOUNT_PANES, { ...previous, [ACCOUNTS]: state });
    },
    drop: async isClosed => {
      if (!isClosed()) return;
      const panes = { ...(await $.state.get(ACCOUNT_PANES)).value };
      if (!isClosed()) return;
      delete panes[ACCOUNTS];
      await $.state.set(ACCOUNT_PANES, panes);
    },
  };
}

async function queueRun($: EngineInterface, args: string[], isOpen = () => true) {
  const home = await $.env.get('HOME');
  if (!home || !isOpen()) throw new Error('Build queue could not be read.');
  const result = await $.process.run([`${home}/.claude/scripts/lib/py`,
    `${home}/.claude/scripts/build_queue/build_queue.py`, ...args]);
  if (!isOpen()) throw new Error('Build queue could not be read.');
  if (args[0] === 'bump' && result.exitCode === 3 && !result.isStdoutTruncated) {
    const refusal = result.stdout.trim();
    if (/^build queue: [^\r\n]+$/.test(refusal)) return refusal;
  }
  if (result.exitCode !== 0 || result.isStdoutTruncated) throw new Error('Build queue could not be read.');
  return result.stdout;
}

type QueueReady = Extract<QueuePaneState, { kind: 'queue' }>;
function validQueueLines(value: unknown): value is QueueLines {
  if (!value || typeof value !== 'object') return false;
  const data = value as QueueLines;
  const tones = ['default', 'success', 'error', 'warning', 'inactive'];
  return Number.isInteger(data.columns) && data.columns > 0 && Number.isFinite(data.as_of)
    && Array.isArray(data.lines) && data.lines.every(line => Array.isArray(line) && line.every(
      part => part && typeof part.text === 'string' && tones.includes(part.tone) && typeof part.bold === 'boolean'));
}
function normalizeQueueState(value: unknown): QueuePaneState {
  const saved = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  const selector = typeof saved.selector === 'string' ? saved.selector : null;
  if (saved.kind === 'queue' && validQueueLines(saved.data)) return { kind: 'queue', data: saved.data, selector };
  if (saved.kind === 'failed') return { kind: 'failed', reason: 'Build queue could not be read.', selector };
  return { kind: 'loading', selector };
}
type QueueOpening = { kind: 'saved' } | { kind: 'view' } | { kind: 'card'; selector: string };
function queuePaneCalls($: EngineInterface, opening: QueueOpening = { kind: 'saved' }): TablePaneCalls<QueueReady, void> {
  return {
    open: () => $.ui.open({ id: QUEUE, title: 'Build queue', closeOnEscape: true }),
    every: callback => $.clock.every(10_000, callback),
    stat: path => $.fs.stat(path),
    prepare: async isOpen => {
      const panes = (await $.state.get(QUEUE_PANES)).value ?? {};
      if (!isOpen()) return;
      const state: QueuePaneState = opening.kind === 'saved' ? normalizeQueueState(panes[QUEUE])
        : { kind: 'loading', selector: opening.kind === 'card' ? opening.selector : null };
      await $.state.set(QUEUE_PANES, { ...panes, [QUEUE]: state });
    },
    load: async (...[, isOpen]) => {
      const state = normalizeQueueState((await $.state.get(QUEUE_PANES)).value?.[QUEUE]);
      if (!isOpen()) throw new Error('Pane closed.');
      const args = ['--json', '--columns', String(queueColumns)];
      const selector = state.selector;
      if (selector !== null) args.push(selector);
      const data: unknown = JSON.parse(await queueRun($, args, isOpen));
      if (!validQueueLines(data)) throw new Error('Invalid queue lines.');
      return { kind: 'queue', data, selector };
    },
    advance: async state => state,
    read: async () => normalizeQueueState((await $.state.get(QUEUE_PANES)).value?.[QUEUE]),
    put: async (state, isOpen) => {
      if (!isOpen()) return;
      const previous = (await $.state.get(QUEUE_PANES)).value ?? {};
      if (!isOpen()) return;
      const saved = normalizeQueueState(previous[QUEUE]);
      if (state.kind === 'loading' && saved.kind === 'loading') return;
      const selected: QueuePaneState = state.kind === 'queue' ? state : { ...state, selector: saved.selector };
      await $.state.set(QUEUE_PANES, { ...previous, [QUEUE]: selected });
    },
    drop: async isClosed => {
      if (!isClosed()) return;
      const panes = { ...(await $.state.get(QUEUE_PANES)).value };
      if (!isClosed()) return;
      delete panes[QUEUE];
      await $.state.set(QUEUE_PANES, panes);
    },
  };
}

export const register: Register = on => {
  const opened = new Map<string, OpenPane>();
  const accounts = tablePane<AccountsReady, AccountsZone>({ failure: 'Stored accounts status could not be read.', refresh: 'changed' });
  const queue = tablePane<QueueReady, void>({ failure: 'Build queue could not be read.', refresh: 'always' });

  on('session.start', async ($, e, next) => {
    const accountsGeneration = accounts.checkpoint();
    const queueGeneration = queue.checkpoint();
    await $.command.register({ name: ACCOUNTS, description: 'Show the stored accounts pane' });
    const panes = await $.ui.panes();
    if (panes.some(pane => pane.id === PANE)) {
      startTimer($, opened, PANE, e.cwd, await $.session.id());
      const active = opened.get(PANE)!;
      const state = await readPane($, PANE);
      if (state?.kind === 'board' || state?.kind === 'unit') {
        active.paths = snapshotPaths(state.data);
        active.selection = state.kind === 'unit' ? { kind: 'unit', production: state.production, unit: state.unit } : { kind: 'board' };
      }
      await tick($, opened, PANE);
    }
    await Promise.all([
      panes.some(pane => pane.id === ACCOUNTS) ? accounts.resume(accountsPaneCalls($), accountsGeneration) : Promise.resolve(),
      panes.some(pane => pane.id === QUEUE) ? queue.resume(queuePaneCalls($), queueGeneration) : Promise.resolve(),
    ]);
    return next(e);
  });
  on('command.run', { command: 'showrunner:unit_board' }, async ($, e) => {
    const wanted = e.args.trim();
    if (wanted === 'close') {
      await $.ui.close({ id: PANE });
      return { text: 'Unit board closed.' };
    }
    const cwd = await $.session.cwd();
    const session = await $.session.id();
    opened.get(PANE)?.timer.cancel();
    opened.delete(PANE);
    await $.ui.open({ id: PANE, title: 'Units', closeOnEscape: true });
    startTimer($, opened, PANE, cwd, session);
    const active = opened.get(PANE)!;
    const isOpen = () => opened.get(PANE) === active;
    active.busy = true;
    await put($, PANE, { kind: 'loading' }, isOpen);
    try {
      const data = await loadBoard(boardCalls($), cwd, session, isOpen, path => retainWatch(active, path));
      if (isOpen()) {
        active.paths = snapshotPaths(data);
        active.selection = commandSelection(data, wanted);
        const now = await $.clock.now() / 1000;
        await put($, PANE, viewState(data, active.selection, now), isOpen);
      }
    } catch (error) {
      await put($, PANE, { kind: 'failed', reason: String(error) }, isOpen);
    } finally { active.busy = false; }
    return { text: 'Unit board opened.' };
  });
  on('command.run', { command: ACCOUNTS }, async ($, e) => {
    if (e.args.trim() === 'close') {
      await $.ui.close({ id: ACCOUNTS });
      return { text: 'Accounts closed.' };
    }
    return { text: await accounts.start(accountsPaneCalls($), true) ? 'Accounts opened.' : 'Accounts closed.' };
  });
  on('command.run', { command: 'build:queue' }, async ($, e) => {
    const wanted = e.args.trim();
    if (wanted === 'close') {
      await $.ui.close({ id: QUEUE });
      return { text: 'Build queue closed.' };
    }
    if (/^bump(?:\s|$)/.test(wanted)) {
      const selector = wanted.slice(4).trim();
      let line: string;
      try {
        if (!selector) throw new Error('Missing selector.');
        const output = await queueRun($, ['bump', selector]);
        line = output.trim();
        if (!line || /[\r\n]/.test(line)) throw new Error('Invalid bump result.');
      } catch { return { text: 'Build queue could not be read.' }; }
      try { await queue.refresh(queuePaneCalls($), true); }
      catch { /* A pane refresh failure cannot replace the settled bump result. */ }
      return { text: line };
    }
    queueWidthRefresh = { kind: 'idle' };
    await queue.start(queuePaneCalls($, wanted ? { kind: 'card', selector: wanted } : { kind: 'view' }), true);
    return { text: 'Build queue opened.' };
  });
  on('turn.complete', async ($, e, next) => {
    if (opened.size === 0) return next(e);
    for (const id of opened.keys()) await tick($, opened, id);
    return next(e);
  });
  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) await stop($, opened, e.id);
    if (e.id === ACCOUNTS) await accounts.stop(accountsPaneCalls($));
    if (e.id === QUEUE) await queue.stop(queuePaneCalls($));
    return next(e);
  });
  on('ui.render', { component: 'Pane', requestId: ACCOUNTS }, async ($, e) => {
    const state = (await $.state.get(ACCOUNT_PANES)).value?.[ACCOUNTS] as AccountsPaneState<StoredAccountsStatus> | undefined;
    const elements = $.ui.resolve(e);
    const { Text } = elements;
    if (!state || state.kind === 'loading') return <Text color="inactive">Loading accounts…</Text>;
    if (state.kind === 'failed') return <Text color="error">{state.reason}</Text>;
    return AccountsView(elements, e.props.bodyColumns, state.data, state.zone, state.now);
  });
  on('ui.render', { component: 'Pane', requestId: QUEUE }, async ($, e) => {
    queueColumns = Math.max(1, Math.floor(e.props.bodyColumns));
    const state = normalizeQueueState((await $.state.get(QUEUE_PANES)).value?.[QUEUE]);
    const elements = $.ui.resolve(e);
    const { Text } = elements;
    if (state.kind === 'loading') return <Text color="inactive">Loading build queue…</Text>;
    if (state.kind === 'failed') return <Text color="error">{state.reason}</Text>;
    if (queueColumns !== state.data.columns
      && (queueWidthRefresh.kind === 'idle' || queueWidthRefresh.columns !== queueColumns)) {
      queueWidthRefresh = { kind: 'requested', columns: queueColumns };
      void queue.refresh(queuePaneCalls($), true);
    } else if (queueColumns === state.data.columns) queueWidthRefresh = { kind: 'idle' };
    return QueueView(elements, queueColumns, state.data);
  });
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const state = await readPane($, PANE);
    const elements = $.ui.resolve(e);
    const { Text } = elements;
    if (!state || state.kind === 'loading') return <Text color="inactive">Loading unit board…</Text>;
    if (state.kind === 'failed') return <Text color="error">{state.reason}</Text>;
    if (state.kind === 'unit') {
      const data = state.data.boards.find(board => board.production === state.production);
      const unit = data?.units.find(unit => unit.key === state.unit);
      if (data && unit) return UnitView(elements, e.props.bodyColumns, e.props.scroll.bodyRows, data, unit, state.now,
        () => { void select($, opened, PANE, { kind: 'board' }); });
    }
    return BoardView(elements, e.props.bodyColumns, e.props.scroll.bodyRows, state.data, state.now,
      (production, unit) => { void select($, opened, PANE, { kind: 'unit', production, unit }); });
  });
};
