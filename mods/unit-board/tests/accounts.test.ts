import { describe, expect, mock, test } from 'claude-code/testing';
import type { Engine } from 'claude-code/testing';
import type { CommandRunInput, EngineInterface, On, TurnCompleteInput } from 'claude-code';
import { register } from '../hooks/register';
import type { StoredAccountsStatus } from '../types/accounts';
import { BOARD, CWD, DISCOVERY, drawnText, fixture, hostEngine, NOW, REGISTRY, RUN } from './kit';

const HOME = '/test-home';
const USAGE = `${HOME}/.local/state/agent-notes/usage.json`;
const GROUPS = `${HOME}/.local/state/accounts/groups.json`;
const CACHE = `${HOME}/.local/state/accounts/role_map_cache.json`;
const CONFIG = `${HOME}/.claude/config/accounts.json`;
const INPUTS = [USAGE, GROUPS, CACHE, CONFIG];
const ARGV = [`${HOME}/.claude/scripts/lib/py`, `${HOME}/.claude/scripts/accounts/accounts.py`,
  'status', '--json', '--stored', '--with-login'];
const command = (name = 'accounts-board', args = ''): CommandRunInput => ({ command: name, args,
  origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 170 } });
const start = { cwd: CWD, surface: null, isInteractive: false } as const;
const turn: TurnCompleteInput = { answer: 'finished', reason: 'answer', durationMs: 100,
  isAborted: false, turnId: 'accounts-turn' };
const props = { title: 'Accounts', isFocused: true, bodyColumns: 170,
  scroll: { offset: 0, bodyRows: 60 }, view: {}, placement: 'dock' as const };
const address = (label: string) => `${label}@example.invalid`;
function statusFixture(): StoredAccountsStatus {
  return { accounts: [
    { label: 'Low', tool: 'claude', login: address('low'), roles: ['unit directors', 'showrunners'],
      weekly_left_percent: 9, resets_at: '2026-10-18T18:00:00+00:00', credits: 42,
      use_credits: true, read_at: '2026-10-16T17:59:00+00:00' },
    { label: 'Watch', tool: 'codex', login: address('watch'), roles: ['workers'],
      weekly_left_percent: 24, resets_at: null, credits: 'unlimited', use_credits: false, read_at: null },
    { label: 'Unknown', tool: 'claude', login: null, roles: [], weekly_left_percent: null,
      resets_at: null, credits: null, use_credits: false, read_at: null },
  ], problems: ['Stored reading unavailable for one account.'] };
}
function accountsHost(on: On, missing: readonly string[] = [], environment: Record<string, string> = { USER_ZONE: 'UTC' }) {
  const host = hostEngine(on);
  const clock = mock.clock(on, { now: NOW });
  const variables: Record<string, string> = { HOME, ...environment };
  const zonePath = environment.USER_ZONE_CONF || `${HOME}/.claude/config/user_zone.conf`;
  let zoneConfig = 'zone=Asia/Tokyo\n';
  let zonePending: Promise<void> = Promise.resolve();
  let zoneRelease = () => {};
  let zoneSignal = () => {};
  on('env.get', async (_$, event) => {
    if (event.name === 'USER_ZONE') {
      zoneSignal();
      await zonePending;
    }
    return { value: variables[event.name] };
  });
  const directories = new Set([HOME, `${HOME}/.local`, `${HOME}/.local/state`,
    `${HOME}/.local/state/agent-notes`, `${HOME}/.local/state/accounts`, `${HOME}/.claude`,
    `${HOME}/.claude/config`, DISCOVERY, REGISTRY]);
  const mtimes = new Map<string, number>([...directories, ...INPUTS, BOARD, RUN].map(path => [path, NOW - 1000]));
  mtimes.set(BOARD, NOW);
  for (const path of missing) mtimes.delete(path);
  let data = statusFixture();
  let failure = '';
  let pending: Promise<void> = Promise.resolve();
  let release = () => {};
  let signal = () => {};
  const fs: string[] = [];
  const processes: string[][] = [];
  const escaped: string[] = [];
  const stateWrites: string[] = [];
  let accountStateKind = 'closed';
  on('state.set', { plugin: 'unit-board', key: 'accountsPanes' }, (_$, e, next) => {
    const state = e.value['accounts-board'];
    accountStateKind = state?.kind ?? 'closed';
    stateWrites.push(accountStateKind);
    return next(e);
  });
  const isAccounts = (argv: readonly string[]) => argv.includes(`${HOME}/.claude/scripts/accounts/accounts.py`);
  on('session.cwd', () => ({ value: CWD }));
  on('fs.stat', (_$, e) => {
    fs.push(e.path);
    const mtimeMs = mtimes.get(e.path);
    if (mtimeMs === undefined) throw new Error('Missing fixture input');
    return { value: { kind: directories.has(e.path) ? 'dir' : 'file', size: 0, mtimeMs, isLink: false } };
  });
  on('fs.exists', (_$, e) => { fs.push(e.path); return { value: mtimes.has(e.path) }; });
  on('fs.list', (_$, e) => {
    fs.push(e.path);
    return { value: [...mtimes.keys()].filter(path => path.startsWith(e.path + '/') &&
      !path.slice(e.path.length + 1).includes('/')).map(path => ({ name: path.slice(e.path.length + 1),
        kind: directories.has(path) ? 'dir' as const : 'file' as const, size: 0,
        mtimeMs: mtimes.get(path)!, isLink: false })) };
  });
  on('fs.read', async (_$, e) => {
    fs.push(e.path);
    if (e.path === zonePath) {
      return { value: zoneConfig };
    }
    if (e.path !== BOARD) throw new Error('The accounts pane must consume process JSON only');
    return { value: JSON.stringify(fixture()) };
  });
  on('fs.write', (_$, e) => { fs.push(e.path); throw new Error('Unexpected fixture write'); });
  on('process.run', async (_$, e) => {
    processes.push([...e.argv]);
    if (isAccounts(e.argv)) {
      signal();
      await pending;
      if (failure) {
        const stderr = failure;
        failure = '';
        return { value: { exitCode: 1, stdout: '', stderr, isStdoutTruncated: false, isStderrTruncated: false } };
      }
      return { value: { exitCode: 0, stdout: JSON.stringify(data), stderr: '',
        isStdoutTruncated: false, isStderrTruncated: false } };
    }
    mtimes.set(BOARD, clock.now());
    return { value: { exitCode: 0, stdout: JSON.stringify({ files: [BOARD], focus: null, watch: [REGISTRY] }),
      stderr: '', isStdoutTruncated: false, isStderrTruncated: false } };
  });
  on('session.send', (_$, e) => { escaped.push(e.text); throw new Error('Unexpected message from accounts pane'); });
  return { ...host, clock, fs, mtimes, processes, escaped, stateWrites,
    accountStateKind: () => accountStateKind,
    accountRuns: () => processes.filter(isAccounts),
    boardRuns: () => processes.filter(argv => !isAccounts(argv)),
    replace: (next: StoredAccountsStatus) => { data = next; },
    failNext: () => { failure = `Stored status failed for ${address('fixture')}.`; },
    clear: () => { fs.length = 0; processes.length = 0; stateWrites.length = 0; },
    change: (path: string) => { mtimes.set(path, clock.now() + 1); },
    setZoneConfig: (contents: string) => { zoneConfig = contents; },
    pauseZone: () => {
      const started = new Promise<void>(resolve => { zoneSignal = resolve; });
      zonePending = new Promise<void>(resolve => { zoneRelease = resolve; });
      return { started, finish: () => zoneRelease() };
    },
    pause: () => {
      const started = new Promise<void>(resolve => { signal = resolve; });
      pending = new Promise<void>(resolve => { release = resolve; });
      return { started, finish: () => release() };
    },
  };
}
async function mount($: Engine, surface: 'terminal' | 'desktop' = 'terminal') {
  return $.ui.mount({ plugin: 'unit-board', requestId: 'accounts-board', component: 'Pane', surface, props });
}
function hasSensitivePath(paths: readonly string[]) {
  return paths.some(path => /(?:\.claude\.json|auth\.json|id_token)/.test(path));
}
function accountRowText(tree: unknown, label: string): string {
  if (!tree || typeof tree !== 'object') return '';
  if (Array.isArray(tree)) {
    for (const child of tree) { const found = accountRowText(child, label); if (found) return found; }
    return '';
  }
  const node = tree as Record<string, unknown>;
  const children = node.children;
  if (Array.isArray(children) && children.some(child => drawnText(child).trim() === label)) return drawnText(tree);
  return accountRowText(children, label);
}

describe('accounts pane', () => {
  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: stored status opens a private table with roles and problems`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      const result = await $.command.run(command());
      expect(result.text === 'Accounts opened.').toBe(true);
      expect(result.text?.includes('@') ?? false).toBe(false);
      expect(fake.accountRuns()).toEqual([ARGV]);
      expect(fake.opened).toEqual(['accounts-board']);
      const ui = await mount($, surface);
      const text = drawnText(await ui.drawn());
      for (const word of ['Label', 'Login', 'Roles', 'Weekly left', 'Weekly resets', 'On credits',
        'Credits left', 'Read', 'Low', 'Watch', 'Unknown', 'units', 'showrunners', 'workers',
        'Stored reading unavailable for one account.', 'unlimited', '42']) {
        expect(text.toLowerCase().includes(word.toLowerCase())).toBe(true);
      }
      expect(text.includes(address('low')) && text.includes(address('watch'))).toBe(true);
      expect(text.includes('unit directors, showrunners')).toBe(true);
      expect(hasSensitivePath(fake.fs)).toBe(false);
      expect(fake.escaped.length).toBe(0);
      expect(fake.forbidden).toEqual([]);
    });
  }

  for (const config of ['repository', 'override']) {
    test(`${config} zone configuration formats resets and readings when the environment zone is unset`, async ($, on) => {
      const environment = config === 'override' ? { USER_ZONE_CONF: '/test-zone.conf' } : {};
      const fake = accountsHost(on, [], environment);
      await $.session.start(start);
      await $.command.run(command());
      const ui = await mount($);
      const text = drawnText(await ui.drawn());
      expect(text.includes('03:00')).toBe(true);
      expect(text.includes('02:59')).toBe(true);
      expect(fake.fs.includes(config === 'override' ? '/test-zone.conf' : `${HOME}/.claude/config/user_zone.conf`)).toBe(true);
      expect(fake.accountRuns()).toEqual([ARGV]);
    });
  }

  test('an empty zone configuration uses the repository default', async ($, on) => {
    const fake = accountsHost(on, [], {});
    fake.setZoneConfig('# no zone setting\n');
    await $.session.start(start);
    await $.command.run(command());
    const ui = await mount($);
    const text = drawnText(await ui.drawn());
    expect(text.includes('14:00')).toBe(true);
    expect(text.includes('13:59')).toBe(true);
  });

  test('an environment zone overrides the stored zone without reading its file', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    const ui = await mount($);
    const text = drawnText(await ui.drawn());
    expect(text.includes('18:00')).toBe(true);
    expect(text.includes('17:59')).toBe(true);
    expect(fake.fs.includes(`${HOME}/.claude/config/user_zone.conf`)).toBe(false);
  });

  for (const role of ['units', 'unit directors']) {
    test(`${role} assignments populate the units summary from stored roles`, async ($, on) => {
      const fake = accountsHost(on);
      const data = statusFixture();
      data.accounts[0]!.roles = [role];
      data.accounts[1]!.roles = ['showrunners'];
      fake.replace(data);
      await $.session.start(start);
      await $.command.run(command());
      const ui = await mount($);
      const text = drawnText(await ui.drawn());
      expect(/units:\s+Low\s*,\s+showrunners:\s+Watch/.test(text)).toBe(true);
    });
  }

  test('closing accounts acknowledges closure without an address', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    const result = await $.command.run(command('accounts-board', 'close'));
    expect(result.text === 'Accounts closed.').toBe(true);
    expect(result.text?.includes('@') ?? false).toBe(false);
    expect(fake.closed).toEqual(['accounts-board']);
  });

  test('closing during startup zone lookup cannot restart accounts while the board keeps refreshing', async ($, on) => {
    const fake = accountsHost(on, [], {});
    await $.session.start(start);
    await $.command.run(command('unit-board'));
    await $.command.run(command());
    const pending = fake.pauseZone();
    const restarting = $.session.start(start);
    await pending.started;
    await $.command.run(command('accounts-board', 'close'));
    fake.clear();
    pending.finish();
    await restarting;
    await fake.clock.advance(60_000);
    expect(fake.accountStateKind()).toBe('closed');
    expect(fake.stateWrites).toEqual([]);
    expect(fake.accountRuns()).toEqual([]);
    expect(fake.fs.some(path => INPUTS.includes(path))).toBe(false);
    expect(fake.fs.includes(`${HOME}/.claude/config/user_zone.conf`)).toBe(false);
    fake.change(RUN);
    await fake.clock.advance(10_000);
    expect(fake.boardRuns().length).toBe(1);
    expect(fake.fs.includes(RUN)).toBe(true);
    expect(fake.accountRuns()).toEqual([]);
  });

  test('startup cancelled during the zone lookup creates no replacement timer', async () => {
    type Hook = (services: EngineInterface, event: unknown, next: (event: unknown) => Promise<unknown>) => unknown;
    const hooks = new Map<string, Hook>();
    const collect = (event: string, ...arguments_: unknown[]) => {
      hooks.set(event, arguments_.at(-1) as Hook);
      return { catch: () => {} };
    };
    register(collect as On, {});
    let finish: (zone: string) => void = () => {};
    let signal = () => {};
    const lookup = new Promise<string>(resolve => { finish = resolve; });
    const started = new Promise<void>(resolve => { signal = resolve; });
    const calls: string[] = [];
    const services = {
      command: { register: async () => ({}) },
      ui: { panes: async () => [{ id: 'accounts-board' }] },
      env: { get: async () => { signal(); return lookup; } },
      state: { get: async () => ({ value: {} }), set: async () => { calls.push('state'); } },
      clock: { every: () => { calls.push('timer'); return { cancel: () => {} }; } },
      fs: { read: async () => { calls.push('read'); return ''; }, stat: async () => { calls.push('stat'); return { mtimeMs: NOW }; } },
      process: { run: async () => { calls.push('process'); throw new Error('Closed pane ran a process'); } },
    } as unknown as EngineInterface;
    const dispatch = async (event: string, input: unknown) => {
      const hook = hooks.get(event);
      if (!hook) throw new Error('Missing lifecycle hook');
      return hook(services, input, async value => value);
    };
    const restarting = dispatch('session.start', start);
    await started;
    await dispatch('ui.close', { id: 'accounts-board', origin: { kind: 'person' } });
    calls.length = 0;
    finish('UTC');
    await restarting;
    expect(calls).toEqual([]);
  });

  test('sibling changes beside existing inputs do not reread stored accounts', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    fake.clear();
    fake.change(`${HOME}/.local/state/accounts`);
    fake.change(`${HOME}/.local/state/accounts/roles.jsonl`);
    await fake.clock.advance(60_000);
    expect(fake.accountRuns()).toEqual([]);
    expect(fake.stateWrites).toEqual([]);
    expect(fake.fs.includes(`${HOME}/.local/state/accounts`)).toBe(false);
  });

  test('nullable readings and login stay unavailable instead of becoming zero', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    const ui = await mount($);
    const row = accountRowText(await ui.drawn(), 'Unknown');
    expect(row.includes('Unknown')).toBe(true);
    expect(/(?:—|unavailable|unknown)/i.test(row.replace('Unknown', ''))).toBe(true);
    expect(row.includes('0%')).toBe(false);
    expect(row.includes('null') || row.includes('undefined')).toBe(false);
    expect(fake.accountRuns()).toEqual([ARGV]);
  });

  test('weekly thresholds use error below ten and warning below twenty-five only', async ($, on) => {
    const fake = accountsHost(on);
    const data = statusFixture();
    data.accounts = [9, 10, 24, 25].map(value => ({ ...data.accounts[0]!,
      label: `Level ${value}`, weekly_left_percent: value }));
    fake.replace(data);
    await $.session.start(start);
    await $.command.run(command());
    const ui = await mount($);
    for (const [value, tone] of [[9, 'error'], [10, 'warning'], [24, 'warning']] as const) {
      const cells = await ui.findAll({ type: 'Text', text: new RegExp(`^${value}(?:\\.0)?\\s*%$`) });
      expect(cells.some(cell => cell.props.color === tone)).toBe(true);
    }
    const healthy = await ui.findAll({ type: 'Text', text: /^25(?:\.0)?\s*%$/ });
    expect(healthy.length).toBeGreaterThan(0);
    expect(healthy.some(cell => cell.props.color === 'warning' || cell.props.color === 'error')).toBe(false);
  });

  for (const source of INPUTS) {
    test(`a changed ${source.split('/').at(-1)} refreshes once and unchanged ticks stay quiet`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      await $.command.run(command());
      const ui = await mount($);
      const changed = statusFixture();
      changed.accounts[0]!.roles = ['reviewers'];
      fake.replace(changed);
      fake.change(source);
      fake.clear();
      await fake.clock.advance(10_000);
      expect(fake.accountRuns()).toEqual([ARGV]);
      expect(drawnText(await ui.drawn()).includes('reviewers')).toBe(true);
      fake.clear();
      await fake.clock.advance(20_000);
      await $.turn.complete(turn);
      expect(fake.processes).toEqual([]);
      expect(hasSensitivePath(fake.fs)).toBe(false);
    });
  }

  test('an initially missing input stays quiet, then creation and deletion each refresh once', async ($, on) => {
    const fake = accountsHost(on, [GROUPS]);
    await $.session.start(start);
    await $.command.run(command());
    fake.clear();
    await fake.clock.advance(20_000);
    expect(fake.processes).toEqual([]);
    fake.change(GROUPS);
    fake.change(`${HOME}/.local/state/accounts`);
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.processes).toEqual([]);
    fake.mtimes.delete(GROUPS);
    fake.change(`${HOME}/.local/state/accounts`);
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    fake.clear();
    await fake.clock.advance(20_000);
    expect(fake.processes).toEqual([]);
  });

  test('closed accounts perform no file or process work and turn completion never refreshes them', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.turn.complete(turn);
    await fake.clock.advance(60_000);
    expect(fake.fs).toEqual([]);
    expect(fake.processes).toEqual([]);
    await $.command.run(command());
    fake.change(USAGE);
    fake.clear();
    await $.turn.complete(turn);
    expect(fake.fs).toEqual([]);
    expect(fake.processes).toEqual([]);
    await $.command.run(command('accounts-board', 'close'));
    fake.clear();
    await fake.clock.advance(60_000);
    await $.turn.complete(turn);
    expect(fake.fs).toEqual([]);
    expect(fake.processes).toEqual([]);
  });

  for (const close of ['accounts-board', 'unit-board']) {
    test(`closing ${close} preserves the other pane refresh loop`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      await $.command.run(command('unit-board'));
      await $.command.run(command());
      await $.command.run(command(close, 'close'));
      fake.clear();
      fake.change(USAGE);
      fake.change(RUN);
      await fake.clock.advance(10_000);
      expect(fake.accountRuns().length).toBe(close === 'accounts-board' ? 0 : 1);
      expect(fake.boardRuns().length).toBe(close === 'unit-board' ? 0 : 1);
      expect(fake.fs.includes(USAGE)).toBe(close !== 'accounts-board');
      expect(fake.fs.includes(RUN)).toBe(close !== 'unit-board');
      await $.command.run(command(close === 'accounts-board' ? 'unit-board' : 'accounts-board', 'close'));
      fake.clear();
      await fake.clock.advance(60_000);
      expect(fake.fs).toEqual([]);
      expect(fake.processes).toEqual([]);
    });
  }

  for (const initial of [true, false]) {
    test(`${initial ? 'initial load' : 'refresh'} failure shows its reason and recovers on a watched change`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      if (!initial) await $.command.run(command());
      fake.failNext();
      if (initial) await $.command.run(command());
      else { fake.change(USAGE); await fake.clock.advance(10_000); }
      const ui = await mount($);
      expect(fake.accountStateKind()).toBe('failed');
      const failed = drawnText(await ui.drawn());
      expect(/failed|could not.*read/i.test(failed)).toBe(true);
      expect(failed.includes('@')).toBe(false);
      fake.change(USAGE);
      await fake.clock.advance(10_000);
      const text = drawnText(await ui.drawn());
      expect(fake.accountStateKind()).toBe('accounts');
      expect(text.includes('Weekly left')).toBe(true);
      expect(fake.accountRuns().every(argv => JSON.stringify(argv) === JSON.stringify(ARGV))).toBe(true);
    });
  }

  test('session restart resumes the existing accounts pane without opening it twice', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    await $.session.start(start);
    fake.clear();
    fake.change(USAGE);
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(fake.opened).toEqual(['accounts-board']);
  });

  for (const refresh of [false, true]) {
    test(`closing during pending ${refresh ? 'refresh' : 'open'} leaves no state and keeps the board live`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      await $.command.run(command('unit-board'));
      if (refresh) await $.command.run(command());
      const pending = fake.pause();
      let opening: Promise<unknown> = Promise.resolve();
      if (refresh) { fake.change(USAGE); opening = fake.clock.advance(10_000); }
      else opening = $.command.run(command());
      await pending.started;
      await $.command.run(command('accounts-board', 'close'));
      fake.clear();
      pending.finish();
      await opening;
      await fake.clock.settle();
      expect(fake.accountStateKind()).toBe('closed');
      expect(fake.stateWrites.length).toBe(0);
      expect(fake.fs).toEqual([]);
      fake.change(RUN);
      await fake.clock.advance(10_000);
      expect(fake.accountRuns()).toEqual([]);
      expect(fake.boardRuns().length).toBe(1);
    });
  }
});
