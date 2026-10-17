import type { BoardData } from '../types/board';
import type { EngineInterface, Register, Timer } from 'claude-code';
import type { AccountsPaneState, BoardSnapshot, PaneState } from '../types';
import { loadAccounts, loadBoard, refreshIfChanged, resolveUserZone, snapshotPaths } from './data';
import type { AccountsLoader, BoardLoader, RefreshPaths, UserZoneSettings } from './data';
import { BoardView } from './board';
import { UnitView } from './unit';
import { AccountsView } from './accounts';
import type { StoredAccountsStatus } from '../types/accounts';

const PANE = 'unit-board';
const ACCOUNTS = 'accounts-board';
const ACCOUNT_PANES = { plugin: 'unit-board', key: 'accountsPanes' } as const;
type OpenAccountsPane = { timer: Timer; busy: boolean; paths: RefreshPaths[]; zone: string };
const PANES = { plugin: 'unit-board', key: 'panes' } as const;

type Selection = { kind: 'board' } | { kind: 'unit'; production: string; unit: string };
type OpenPane = { timer: Timer; cwd: string; busy: boolean; paths: RefreshPaths[]; selection: Selection };

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
  const key = wanted || data.focus;
  if (!key) return { kind: 'board' };
  const focused = data.focus ? data.boards[0] : undefined;
  const board = focused?.units.some(unit => unit.key === key) ? focused : data.boards.find(board => board.units.some(unit => unit.key === key));
  return board ? { kind: 'unit', production: board.production, unit: key } : { kind: 'board' };
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
      () => loadBoard(boardCalls($), active.cwd, isOpen, path => retainWatch(active, path)), async data => {
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
const startTimer = ($: EngineInterface, opened: Map<string, OpenPane>, id: string, cwd: string) => {
  const previous = opened.get(id);
  previous?.timer.cancel();
  opened.set(id, { cwd, busy: false, paths: previous?.paths ?? [], selection: previous?.selection ?? { kind: 'board' },
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

const putAccounts = async ($: EngineInterface, state: AccountsPaneState<StoredAccountsStatus>, isOpen: () => boolean) => {
  if (!isOpen()) return;
  const previous = (await $.state.get(ACCOUNT_PANES)).value ?? {};
  if (isOpen()) await $.state.set(ACCOUNT_PANES, { ...previous, [ACCOUNTS]: state });
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
const readAccounts = async ($: EngineInterface, opened: Map<string, OpenAccountsPane>, active: OpenAccountsPane) => {
  const isOpen = () => opened.get(ACCOUNTS) === active;
  try {
    const data = await loadAccounts(accountsCalls($), isOpen, paths => { if (isOpen()) active.paths = paths; });
    await putAccounts($, { kind: 'accounts', data, zone: active.zone }, isOpen);
  } catch {
    // Never include process output or a login in an error or command result.
    await putAccounts($, { kind: 'failed', reason: 'Stored accounts status could not be read.' }, isOpen);
  }
};
const tickAccounts = async ($: EngineInterface, opened: Map<string, OpenAccountsPane>) => {
  const active = opened.get(ACCOUNTS);
  if (!active || active.busy) return;
  const isOpen = () => opened.get(ACCOUNTS) === active;
  active.busy = true;
  try {
    await refreshIfChanged({ stat: path => $.fs.stat(path) }, active.paths,
      () => loadAccounts(accountsCalls($), isOpen, paths => { if (isOpen()) active.paths = paths; }),
      data => putAccounts($, { kind: 'accounts', data, zone: active.zone }, isOpen), isOpen);
  } catch {
    await putAccounts($, { kind: 'failed', reason: 'Stored accounts status could not be read.' }, isOpen);
  } finally { active.busy = false; }
};
const startAccounts = ($: EngineInterface, opened: Map<string, OpenAccountsPane>, zone: string) => {
  const previous = opened.get(ACCOUNTS);
  previous?.timer.cancel();
  const active: OpenAccountsPane = { busy: false, paths: previous?.paths ?? [], zone,
    timer: $.clock.every(10_000, () => { void tickAccounts($, opened); }) };
  opened.set(ACCOUNTS, active);
  return active;
};
const stopAccounts = async ($: EngineInterface, opened: Map<string, OpenAccountsPane>) => {
  opened.get(ACCOUNTS)?.timer.cancel();
  opened.delete(ACCOUNTS);
  const panes = { ...(await $.state.get(ACCOUNT_PANES)).value };
  delete panes[ACCOUNTS];
  await $.state.set(ACCOUNT_PANES, panes);
};

export const register: Register = on => {
  const opened = new Map<string, OpenPane>();
  const accounts = new Map<string, OpenAccountsPane>();
  let accountsLifecycle = 0;

  on('session.start', async ($, e, next) => {
    const lifecycle = ++accountsLifecycle;
    await $.command.register({ name: PANE, description: 'Show the production board pane' });
    await $.command.register({ name: ACCOUNTS, description: 'Show the stored accounts pane' });
    const panes = await $.ui.panes();
    if (panes.some(pane => pane.id === PANE)) {
      startTimer($, opened, PANE, e.cwd);
      const active = opened.get(PANE)!;
      const state = await readPane($, PANE);
      if (state?.kind === 'board' || state?.kind === 'unit') {
        active.paths = snapshotPaths(state.data);
        active.selection = state.kind === 'unit' ? { kind: 'unit', production: state.production, unit: state.unit } : { kind: 'board' };
      }
      await tick($, opened, PANE);
    }
    if (panes.some(pane => pane.id === ACCOUNTS)) {
      const zone = await resolveUserZone(zoneSettings($), () => accountsLifecycle === lifecycle);
      if (zone.kind === 'resolved' && accountsLifecycle === lifecycle) {
        const active = startAccounts($, accounts, zone.name);
        if (active.paths.length) await tickAccounts($, accounts);
        else {
          active.busy = true;
          try { await readAccounts($, accounts, active); } finally { active.busy = false; }
        }
      }
    }
    return next(e);
  });
  on('command.run', { command: PANE }, async ($, e) => {
    const wanted = e.args.trim();
    if (wanted === 'close') {
      await $.ui.close({ id: PANE });
      return { text: 'Unit board closed.' };
    }
    const cwd = await $.session.cwd();
    opened.get(PANE)?.timer.cancel();
    opened.delete(PANE);
    await $.ui.open({ id: PANE, title: 'Units', closeOnEscape: true });
    startTimer($, opened, PANE, cwd);
    const active = opened.get(PANE)!;
    const isOpen = () => opened.get(PANE) === active;
    active.busy = true;
    await put($, PANE, { kind: 'loading' }, isOpen);
    try {
      const data = await loadBoard(boardCalls($), cwd, isOpen, path => retainWatch(active, path));
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
    const lifecycle = ++accountsLifecycle;
    accounts.get(ACCOUNTS)?.timer.cancel();
    accounts.delete(ACCOUNTS);
    const zone = await resolveUserZone(zoneSettings($), () => accountsLifecycle === lifecycle);
    if (zone.kind === 'cancelled' || accountsLifecycle !== lifecycle) return { text: 'Accounts closed.' };
    await $.ui.open({ id: ACCOUNTS, title: 'Accounts', closeOnEscape: true });
    if (accountsLifecycle !== lifecycle) return { text: 'Accounts closed.' };
    const active = startAccounts($, accounts, zone.name);
    const isOpen = () => accounts.get(ACCOUNTS) === active;
    active.busy = true;
    try {
      await putAccounts($, { kind: 'loading' }, isOpen);
      await readAccounts($, accounts, active);
    } finally { active.busy = false; }
    return { text: 'Accounts opened.' };
  });
  on('turn.complete', async ($, e, next) => {
    if (opened.size === 0) return next(e);
    for (const id of opened.keys()) await tick($, opened, id);
    return next(e);
  });
  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) await stop($, opened, e.id);
    if (e.id === ACCOUNTS) {
      accountsLifecycle++;
      await stopAccounts($, accounts);
    }
    return next(e);
  });
  on('ui.render', { component: 'Pane', requestId: ACCOUNTS }, async ($, e) => {
    const state = (await $.state.get(ACCOUNT_PANES)).value?.[ACCOUNTS] as AccountsPaneState<StoredAccountsStatus> | undefined;
    const elements = $.ui.resolve(e);
    const { Text } = elements;
    if (!state || state.kind === 'loading') return <Text color="subtle">Loading accounts…</Text>;
    if (state.kind === 'failed') return <Text color="error">{state.reason}</Text>;
    return AccountsView(elements, state.data, state.zone);
  });
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const state = await readPane($, PANE);
    const elements = $.ui.resolve(e);
    const { Text } = elements;
    if (!state || state.kind === 'loading') return <Text color="subtle">Loading unit board…</Text>;
    if (state.kind === 'failed') return <Text color="error">{state.reason}</Text>;
    if (state.kind === 'unit') {
      const data = state.data.boards.find(board => board.production === state.production);
      const unit = data?.units.find(unit => unit.key === state.unit);
      if (data && unit) return UnitView(elements, e.props.bodyColumns, data, unit, state.now,
        () => { void select($, opened, PANE, { kind: 'board' }); });
    }
    return BoardView(elements, e.props.bodyColumns, e.props.scroll.bodyRows, state.data, state.now,
      (production, unit) => { void select($, opened, PANE, { kind: 'unit', production, unit }); });
  });
};
