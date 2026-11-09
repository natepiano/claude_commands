import { describe, expect, mock, test } from 'claude-code/testing';
import type { Engine } from 'claude-code/testing';
import type { CommandRunInput, EngineInterface, On, TurnCompleteInput } from 'claude-code';
import { register } from '../hooks/register';
import { accountDisplay } from '../hooks/accounts';
import { cellWidth } from '../hooks/geometry';
import type { StoredAccountsStatus } from '../types/accounts';
import { CWD, drawnText, element, elements, hostEngine, NOW, rowLines, styledText } from './kit';

const HOME = '/test-home';
const USAGE = `${HOME}/.local/state/agent-notes/usage.json`;
const GROUPS = `${HOME}/.local/state/accounts/groups.json`;
const CACHE = `${HOME}/.local/state/accounts/role_map_cache.json`;
const CONFIG = `${HOME}/.claude/config/accounts.json`;
const INPUTS = [USAGE, GROUPS, CACHE, CONFIG];
const ARGV = [`${HOME}/.claude/scripts/lib/py`, `${HOME}/.claude/scripts/accounts/accounts.py`,
  'status', '--json', '--stored'];
const command = (name = 'showrunner:accounts', args = ''): CommandRunInput => ({ command: name, args,
  origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 170 } });
const start = { cwd: CWD, surface: null, isInteractive: false } as const;
const turn: TurnCompleteInput = { answer: 'finished', reason: 'answer', durationMs: 100,
  isAborted: false, turnId: 'accounts-turn' };
const props = { title: 'Accounts', isFocused: true, bodyColumns: 170,
  scroll: { offset: 0, bodyRows: 60 }, view: {}, placement: 'dock' as const };
const address = (label: string) => `${label}@example.invalid`;
function statusFixture(): StoredAccountsStatus {
  return { accounts: [
    { label: 'Low', tool: 'claude', roles: ['unit directors', 'showrunners'], role_counts: [{ busy: 1, idle: 1, paused: 0, exited: 1, total: 3 }, { busy: 1, idle: 1, paused: 0, exited: 0, total: 2 }],
      weekly_left_percent: 9, resets_at: '2026-10-18T18:00:00+00:00', credits: 42,
      use_credits: true, read_ok: true, credits_in_use: true, read_at: '2026-10-16T17:59:00+00:00' },
    { label: 'Watch', tool: 'codex', roles: ['workers'], role_counts: [{ busy: 2, idle: 2, paused: 0, exited: 0, total: 4 }],
      weekly_left_percent: 24, resets_at: null, credits: 'unlimited', use_credits: false, read_ok: false, credits_in_use: false, read_at: null },
    { label: 'Unknown', tool: 'claude', roles: [], role_counts: [], weekly_left_percent: null,
      resets_at: null, credits: null, use_credits: false, read_ok: false, credits_in_use: false, read_at: null },
  ], problems: ['Stored reading unavailable for one account.'] };
}
const OLD_HEADINGS = ['Login', 'Weekly left', 'Weekly resets', 'On credits', 'Credits left'];
/** Four Claude and three Codex accounts; deliberately unsorted and mixed case. */
function readableFixture(): StoredAccountsStatus {
  return { accounts: [
    { label: 'Gamma', tool: 'claude', roles: ['workers'], role_counts: [{ busy: 1, idle: 2, paused: 0, exited: 1, total: 4 }], weekly_left_percent: 74,
      resets_at: '2026-10-24T18:00:00Z', credits: null, use_credits: false, read_ok: true, credits_in_use: false,
      read_at: '2026-10-16T17:55:00Z' },
    { label: 'beta', tool: 'codex', roles: ['workers'], role_counts: [{ busy: 7, idle: 5, paused: 0, exited: 0, total: 12 }], weekly_left_percent: 20.2,
      resets_at: '2026-10-17T18:00:00Z', credits: 13446.2, use_credits: false, read_ok: false, credits_in_use: false,
      read_at: '2026-10-16T17:48:00Z' },
    { label: 'Echo', tool: 'claude', roles: ['showrunners', 'unit directors'], role_counts: [{ busy: 0, idle: 0, paused: 0, exited: 0, total: 0 }, { busy: 0, idle: 0, paused: 0, exited: 0, total: 0 }], weekly_left_percent: null,
      resets_at: null, credits: null, use_credits: false, read_ok: false, credits_in_use: false, read_at: null },
    { label: 'aLpha', tool: 'claude', roles: ['showrunners', 'unit directors'], role_counts: [{ busy: 1, idle: 0, paused: 0, exited: 1, total: 2 }, { busy: 2, idle: 1, paused: 0, exited: 0, total: 3 }], weekly_left_percent: 60.4,
      resets_at: '2026-10-16T18:00:00Z', credits: 'unlimited', use_credits: true, read_ok: true, credits_in_use: true,
      read_at: '2026-10-16T17:59:15Z' },
    { label: 'Foxtrot', tool: 'codex', roles: ['spare'], role_counts: [null], weekly_left_percent: 90,
      resets_at: '2026-10-22T18:00:00Z', credits: null, use_credits: true, read_ok: true, credits_in_use: false,
      read_at: '2026-10-16T17:56:00Z' },
    { label: 'Charlie', tool: 'claude', roles: ['workers'], role_counts: [{ busy: 0, idle: 1, paused: 0, exited: 0, total: 1 }], weekly_left_percent: 8.4,
      resets_at: '2026-10-18T18:00:00Z', credits: null, use_credits: false, read_ok: true, credits_in_use: false,
      read_at: '2026-10-16T16:00:00Z' },
    { label: 'Delta', tool: 'codex', roles: ['spare'], role_counts: [null], weekly_left_percent: 25,
      resets_at: '2026-10-23T18:00:00Z', credits: null, use_credits: false, read_ok: true, credits_in_use: false,
      read_at: '2026-10-13T18:00:00Z' },
  ], problems: [
    'Stored weekly reading is unavailable for the spare account; keep its previous assignment until another reading arrives.',
    'The role cache has an old showrunner assignment that needs refreshing before the next production starts.',
  ] };
}
// These literal rows specify the columns, role order, totals, and narrow facts.
const TABLE_DRAWN_LINES: Record<number, readonly string[]> = {
  "30": [
    "● aLpha",
    "  60% left · role showrunners",
    "  busy 1 · idle 0 · paused 0",
    "  exited 1 · total 2",
    "  resets 14:00",
    "  credits unlimited · enabled",
    "  role unit directors · busy 2",
    "  idle 1 · paused 0 · exited 0",
    "  total 3",
    "● beta",
    "  20% left · role workers",
    "  busy 7 · idle 5 · paused 0",
    "  exited 0 · total 12",
    "  resets Sat 14:00",
    "  credits 13,446",
    "● Charlie",
    "  8% left · role workers",
    "  busy 0 · idle 1 · paused 0",
    "  exited 0 · total 1",
    "  resets Sun 14:00",
    "● Delta",
    "  25% left · role spare",
    "  resets 23 Oct",
    "● Echo",
    "  — left · role showrunners",
    "  busy 0 · idle 0 · paused 0",
    "  exited 0 · total 0",
    "  role unit directors · busy 0",
    "  idle 0 · paused 0 · exited 0",
    "  total 0",
    "● Foxtrot",
    "  90% left · role spare",
    "  resets Thu 14:00 · enabled",
    "● Gamma",
    "  74% left · role workers",
    "  busy 1 · idle 2 · paused 0",
    "  exited 1 · total 4",
    "  resets 24 Oct",
    "total",
    "  busy 11 · idle 9 · paused 0",
    "  exited 2 · total 22",
    "",
    "Problems",
    "• Stored weekly reading is",
    "  unavailable for the spare",
    "  account; keep its previous",
    "  assignment until another",
    "  reading arrives.",
    "• The role cache has an old",
    "  showrunner assignment that",
    "  needs refreshing before the",
    "  next production starts."
  ],
  "60": [
    "● aLpha",
    "  60% left · role showrunners · busy 1 · idle 0 · paused 0",
    "  exited 1 · total 2 · resets 14:00 · credits unlimited",
    "  enabled",
    "  role unit directors · busy 2 · idle 1 · paused 0",
    "  exited 0 · total 3",
    "● beta",
    "  20% left · role workers · busy 7 · idle 5 · paused 0",
    "  exited 0 · total 12 · resets Sat 14:00 · credits 13,446",
    "● Charlie",
    "  8% left · role workers · busy 0 · idle 1 · paused 0",
    "  exited 0 · total 1 · resets Sun 14:00",
    "● Delta",
    "  25% left · role spare · resets 23 Oct",
    "● Echo",
    "  — left · role showrunners · busy 0 · idle 0 · paused 0",
    "  exited 0 · total 0",
    "  role unit directors · busy 0 · idle 0 · paused 0",
    "  exited 0 · total 0",
    "● Foxtrot",
    "  90% left · role spare · resets Thu 14:00 · enabled",
    "● Gamma",
    "  74% left · role workers · busy 1 · idle 2 · paused 0",
    "  exited 1 · total 4 · resets 24 Oct",
    "total",
    "  busy 11 · idle 9 · paused 0 · exited 2 · total 22",
    "",
    "Problems",
    "• Stored weekly reading is unavailable for the spare",
    "  account; keep its previous assignment until another",
    "  reading arrives.",
    "• The role cache has an old showrunner assignment that",
    "  needs refreshing before the next production starts."
  ],
  "100": [
    "   account  left  role            busy  idle  paused  exited  total     resets    credits  enabled",
    "●  aLpha     60%  showrunners        1     0       0       1      2      14:00  unlimited  yes",
    "                  unit directors     2     1       0       0      3",
    "●  beta      20%  workers            7     5       0       0     12  Sat 14:00     13,446",
    "●  Charlie    8%  workers            0     1       0       0      1  Sun 14:00",
    "●  Delta     25%  spare                                                 23 Oct",
    "●  Echo        —  showrunners        0     0       0       0      0",
    "                  unit directors     0     0       0       0      0",
    "●  Foxtrot   90%  spare                                              Thu 14:00             yes",
    "●  Gamma     74%  workers            1     2       0       1      4     24 Oct",
    "   total                            11     9       0       2     22",
    "",
    "Problems",
    "• Stored weekly reading is unavailable for the spare account; keep its previous assignment until",
    "  another reading arrives.",
    "• The role cache has an old showrunner assignment that needs refreshing before the next production",
    "  starts."
  ],
  "140": [
    "   account  left  role            busy  idle  paused  exited  total     resets    credits  enabled",
    "●  aLpha     60%  showrunners        1     0       0       1      2      14:00  unlimited  yes",
    "                  unit directors     2     1       0       0      3",
    "●  beta      20%  workers            7     5       0       0     12  Sat 14:00     13,446",
    "●  Charlie    8%  workers            0     1       0       0      1  Sun 14:00",
    "●  Delta     25%  spare                                                 23 Oct",
    "●  Echo        —  showrunners        0     0       0       0      0",
    "                  unit directors     0     0       0       0      0",
    "●  Foxtrot   90%  spare                                              Thu 14:00             yes",
    "●  Gamma     74%  workers            1     2       0       1      4     24 Oct",
    "   total                            11     9       0       2     22",
    "",
    "Problems",
    "• Stored weekly reading is unavailable for the spare account; keep its previous assignment until another reading arrives.",
    "• The role cache has an old showrunner assignment that needs refreshing before the next production starts."
  ]
};
const LABELS = ['aLpha', 'beta', 'Charlie', 'Delta', 'Echo', 'Foxtrot', 'Gamma'];

/** Extra fields can still arrive from an older status producer. */
function legacyLoginFixture() {
  const data = statusFixture();
  return { ...data, accounts: data.accounts.map(account => ({ ...account, login: address(account.label) })) };
}

function textInColor(tree: unknown, color: string): string {
  return styledText(tree).filter(span => span.color === color).map(span => span.text).join('');
}
function accountLines(tree: unknown, label: string): string[] {
  const lines = rowLines(tree);
  const first = lines.findIndex(line => line.startsWith('● ') && (line.slice(1).trimStart() === label || line.slice(1).trimStart().startsWith(label + ' ')));
  if (first < 0) return [];
  const rest = lines.slice(first + 1);
  const next = rest.findIndex(line => line.startsWith('● ') || line === '' || line === 'Problems' || line === 'total' || line.startsWith('   total '));
  return [lines[first]!, ...rest.slice(0, next < 0 ? rest.length : next)];
}
function normalized(lines: readonly string[]): string {
  return lines.join(' ').replace(/\s+/g, ' ').trim();
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
    `${HOME}/.claude/config`]);
  const mtimes = new Map<string, number>([...directories, ...INPUTS].map(path => [path, NOW - 1000]));
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
    const state = e.value['accounts'];
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
    throw new Error('The accounts pane must consume process JSON only');
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
    throw new Error('Unexpected process from the accounts pane');
  });
  on('session.send', (_$, e) => { escaped.push(e.text); throw new Error('Unexpected message from accounts pane'); });
  return { ...host, clock, fs, mtimes, processes, escaped, stateWrites,
    accountStateKind: () => accountStateKind,
    accountRuns: () => processes.filter(isAccounts),
    otherRuns: () => processes.filter(argv => !isAccounts(argv)),
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
  return $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane', surface, props });
}
function hasSensitivePath(paths: readonly string[]) {
  return paths.some(path => /(?:\.claude\.json|auth\.json|id_token)/.test(path));
}
describe('accounts table', () => {
  for (const columns of [140, 50]) {
    test(`${columns} cells: a partial move shows actual counts and selected empty roles`, async ($, on) => {
      const fake = accountsHost(on);
      const data = statusFixture();
      const base = data.accounts[0]!;
      data.accounts = [
        { ...base, label: 'claude2', roles: ['units', 'showrunners'], selected_roles: ['units', 'showrunners'],
          role_counts: [{ busy: 1, idle: 0, paused: 0, exited: 0, total: 1 }, { busy: 0, idle: 0, paused: 0, exited: 0, total: 0 }] },
        { ...base, label: 'claude4', roles: ['units'], selected_roles: [],
          role_counts: [{ busy: 1, idle: 1, paused: 0, exited: 1, total: 3 }] },
      ];
      data.problems = [];
      fake.replace(data);
      await $.command.run(command());
      const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane',
        surface: 'terminal', props: { ...props, bodyColumns: columns } });
      const tree = await ui.drawn();
      const selected = normalized(accountLines(tree, 'claude2')).replace(/ · /g, ' ');
      const previous = normalized(accountLines(tree, 'claude4')).replace(/ · /g, ' ');
      expect(selected).toContain('units ✓');
      expect(selected).toContain('showrunners ✓');
      expect(previous).toContain('units');
      expect(previous).not.toContain('✓');
      if (columns === 140) {
        expect(selected).toContain('units ✓ 1 0 0 0 1');
        expect(selected).toContain('showrunners ✓ 0 0 0 0 0');
        expect(previous).toContain('units 1 1 0 1 3');
      } else {
        expect(selected).toContain('role units ✓ busy 1 idle 0 paused 0 exited 0 total 1');
        expect(selected).toContain('role showrunners ✓ busy 0 idle 0 paused 0 exited 0 total 0');
        expect(previous).toContain('role units busy 1 idle 1 paused 0 exited 1 total 3');
      }
      const lines = rowLines(tree);
      const total = lines.findIndex(line => line.trim().startsWith('total'));
      expect(normalized(lines.slice(total))).toContain(columns === 140 ? 'total 2 1 0 1 4' : 'total busy 2 · idle 1 · paused 0 · exited 1 · total 4');
    });
  }

  test('activity columns preserve busy, idle, paused, exited, and total order', async ($, on) => {
    const fake = accountsHost(on);
    const data = statusFixture();
    data.accounts = [{ ...data.accounts[0]!, roles: ['units'], selected_roles: ['units'],
      role_counts: [{ busy: 1, idle: 2, paused: 3, exited: 4, total: 10 }] }];
    data.problems = [];
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    const lines = rowLines(await ui.drawn());
    expect(lines[0]!.trim().split(/\s+/)).toEqual([
      'account', 'left', 'role', 'busy', 'idle', 'paused', 'exited', 'total', 'resets', 'credits', 'enabled']);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('units ✓ 1 2 3 4 10');
    expect(lines.join(' ')).not.toContain('shell');
  });

  for (const upcoming of [
    ['Upcoming moves:', 'showrunners 1: at 1% → claude3, else claude2', '', 'units 1: at 1% → claude4', '', 'workers 1: at 1% → codex2'],
    ['Upcoming moves: none'],
    ['Upcoming moves: unavailable from natedev; next: remote list failed: fixture host unreachable'],
  ]) {
    test(`upcoming moves appear under the table: ${upcoming[0]}`, async ($, on) => {
      const fake = accountsHost(on);
      const data = statusFixture();
      data.upcoming_moves = upcoming;
      fake.replace(data);
      await $.command.run(command());
      const ui = await mount($);
      const lines = rowLines(await ui.drawn());
      const heading = lines.indexOf(upcoming[0]!);
      const total = lines.findIndex(line => line.trim().startsWith('total'));
      const problems = lines.indexOf('Problems');
      expect(heading).toBeGreaterThan(total);
      expect(problems).toBeGreaterThan(heading);
      expect(lines.slice(heading, heading + upcoming.length)).toEqual(upcoming);
      expect(fake.accountRuns()).toEqual([ARGV]);
    });
  }

  for (const columns of [140, 60, 30]) {
    test(`${columns} cells: the next role has only its own role and counts`, async ($, on) => {
      const fake = accountsHost(on);
      const data = statusFixture();
      data.accounts = [data.accounts[0]!];
      data.problems = [];
      const before = JSON.stringify(data);
      fake.replace(data);
      await $.command.run(command());
      const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane',
        surface: 'terminal', props: { ...props, bodyColumns: columns } });
      const tree = await ui.drawn();
      const rows = accountLines(tree, 'Low');
      const next = rows.findIndex(line => /^(?:role )?showrunners\b/.test(line.trim()));
      expect(next).toBeGreaterThan(0);
      const continuation = normalized(rows.slice(next)).replace(/ · /g, ' ');
      expect(continuation).toBe(columns <= 60
        ? 'role showrunners busy 1 idle 1 paused 0 exited 0 total 2' : 'showrunners 1 1 0 0 2');
      expect(rows.filter(line => line.startsWith('● '))).toHaveLength(1);
      expect(JSON.stringify(data)).toBe(before);
    });
  }

  for (const columns of [140, 50, 30]) {
    test(`${columns} cells: reads combined counts and retains separate machine data`, async ($, on) => {
      const fake = accountsHost(on);
      const data = statusFixture();
      data.accounts = [data.accounts[0]!, data.accounts[1]!];
      data.accounts[0]!.role_counts = [
        { busy: 3, idle: 1, paused: 0, exited: 2, total: 6 }, { busy: 1, idle: 3, paused: 0, exited: 1, total: 5 }];
      data.accounts[1]!.role_counts = [{ busy: 3, idle: 2, paused: 0, exited: 0, total: 5 }];
      data.accounts[0]!.role_counts_by_machine = {
        natedev: { 'unit directors': { busy: 1, idle: 1, paused: 0, exited: 1, total: 3 },
          showrunners: { busy: 1, idle: 1, paused: 0, exited: 0, total: 2 } },
        mac: { 'unit directors': { busy: 2, idle: 0, paused: 0, exited: 1, total: 3 },
          showrunners: { busy: 0, idle: 2, paused: 0, exited: 1, total: 3 } },
      };
      data.accounts[1]!.role_counts_by_machine = {
        natedev: { workers: { busy: 2, idle: 2, paused: 0, exited: 0, total: 4 } },
        mac: { workers: { busy: 1, idle: 0, paused: 0, exited: 0, total: 1 } },
      };
      const before = JSON.stringify(data);
      fake.replace(data);
      await $.command.run(command());
      const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane',
        surface: 'terminal', props: { ...props, bodyColumns: columns } });
      const tree = await ui.drawn();
      const row = normalized(accountLines(tree, 'Low'));
      if (columns === 140) {
        expect(row).toContain('unit directors 3 1 0 2 6');
        expect(row).toContain('showrunners 1 3 0 1 5');
      } else {
        const facts = row.replace(/ · /g, ' ');
        expect(facts).toContain('role unit directors busy 3 idle 1 paused 0 exited 2 total 6');
        expect(facts).toContain('role showrunners busy 1 idle 3 paused 0 exited 1 total 5');
      }
      expect(accountDisplay(data.accounts[1]!).roleCounts).toEqual([
        { kind: 'available', value: { busy: 3, idle: 2, paused: 0, exited: 0, total: 5 } },
      ]);
      const lines = rowLines(tree);
      if (columns === 140) expect(lines.find(line => line.trim().startsWith('total'))!.trim().replace(/\s+/g, ' '))
        .toBe('total 7 6 0 3 16');
      else expect(normalized(lines.slice(lines.indexOf('total')))).toContain(columns === 30 ? 'total busy 7 · idle 6 · paused 0 exited 3' : 'total busy 7 · idle 6 · paused 0 · exited 3');
      expect(JSON.stringify(data)).toBe(before);
      data.accounts[1]!.role_counts_by_machine = {};
      expect(accountDisplay(data.accounts[1]!).roleCounts).toEqual([
        { kind: 'available', value: { busy: 3, idle: 2, paused: 0, exited: 0, total: 5 } },
      ]);
    });
  }

  for (const tool of ['claude', 'codex'] as const) {
    test(`${tool}: unreported roles show zero and spare has no count`, async ($, on) => {
      const fake = accountsHost(on);
      const zero = { busy: 0, idle: 0, paused: 0, exited: 0, total: 0 };
      const data = statusFixture();
      data.accounts = [{ ...data.accounts[0]!, tool, roles: ['units', 'spare'], selected_roles: ['units'],
        role_counts: [zero, null], role_counts_by_machine: { natedev: {}, mac: {} } }];
      fake.replace(data);
      expect(accountDisplay(data.accounts[0]!).roleCounts).toEqual([
        { kind: 'available', value: zero }, { kind: 'unavailable' },
      ]);
      await $.command.run(command());
      const ui = await mount($);
      const lines = rowLines(await ui.drawn());
      const row = lines.find(line => line.includes('Low'))!;
      expect(row.trim().replace(/\s+/g, ' ')).toContain('units ✓ 0 0 0 0 0');
      const exited = lines[0]!.indexOf('exited');
      expect(row.slice(exited, exited + 6).trim()).toBe('0');
      expect(lines.find(line => line.trim().startsWith('spare'))!.trim()).toBe('spare');
    });
  }

  test('unavailable peer counts show the local counts and one problem', async ($, on) => {
    const fake = accountsHost(on);
    const data = statusFixture();
    data.accounts = [data.accounts[1]!];
    data.accounts[0]!.role_counts_by_machine = { mac: { workers: { busy: 2, idle: 2, paused: 0, exited: 0, total: 4 } } };
    data.problems = ['natedev unavailable: counts are mac only'];
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    const text = drawnText(await ui.drawn());
    expect(text.split(data.problems[0]!).length - 1).toBe(1);
    expect(accountDisplay(data.accounts[0]!).roleCounts).toEqual([
      { kind: 'available', value: { busy: 2, idle: 2, paused: 0, exited: 0, total: 4 } },
    ]);
  });

  for (const surface of ['terminal', 'desktop'] as const) {
    for (const columns of [140, 100, 60, 30]) {
      test(`${surface}: exact table form at ${columns} cells keeps every fact and problem`, async ($, on) => {
        const fake = accountsHost(on, [], { USER_ZONE: 'America/New_York' });
        const data = readableFixture();
        const before = JSON.stringify(data);
        fake.replace(data);
        await $.command.run(command());
        const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane', surface,
          props: { ...props, bodyColumns: columns } });
        const tree = await ui.drawn();
        const lines = rowLines(tree);
        expect(lines).toEqual(TABLE_DRAWN_LINES[columns]!);
        expect(lines.filter(line => line.startsWith('● ')).map(line => line.slice(1).trim().split(' ')[0]))
          .toEqual(LABELS);
        for (const line of lines) {
          expect(cellWidth(line)).toBeLessThanOrEqual(columns);
          expect(line).not.toContain('…');
        }
        const spans = styledText(tree);
        for (const label of LABELS) {
          const labels = spans.filter(span => span.text.includes(label));
          expect(labels.length).toBeGreaterThan(0);
          expect(labels.every(span => span.bold && span.color === 'default')).toBe(true);
        }
        for (const heading of ['account', 'left', 'role', 'busy', 'idle', 'paused', 'exited', 'total', 'resets', 'credits', 'enabled']) {
          const headings = spans.filter(span => span.bold && span.text.trim() === heading);
          expect(headings.length).toBe((columns <= 60 ? 0 : 1) + (heading === 'total' ? 1 : 0));
          expect(headings.every(span => span.bold && span.color === 'default')).toBe(true);
        }
        for (const node of elements(tree)) {
          const attributes = element(node.props);
          expect(attributes.color === 'subtle').toBe(false);
          expect(attributes.dimColor === true).toBe(false);
          expect(attributes.wrap === 'truncate-end').toBe(false);
        }
        const problemHeading = lines.indexOf('Problems');
        expect(lines[problemHeading - 1]).toBe('');
        expect(spans.some(span => span.text === 'Problems' && span.bold)).toBe(true);
        const headingSpan = spans.findIndex(span => span.text === 'Problems');
        expect(spans.slice(headingSpan + 1).filter(span => span.text.trim()).every(span => span.color === 'warning')).toBe(true);
        expect(JSON.stringify(data)).toBe(before);
      });
    }
  }

  test('column starts and right edges align across full and two-row tables', async ($, on) => {
    const fake = accountsHost(on, [], { USER_ZONE: 'America/New_York' });
    fake.replace(readableFixture());
    await $.command.run(command());
    for (const columns of [140, 80]) {
      const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane', surface: 'terminal',
        props: { ...props, bodyColumns: columns } });
      const lines = rowLines(await ui.drawn());
      const header = lines[0]!;
      expect(header.indexOf('account')).toBe(3);
      expect(header.indexOf('role')).toBe(18);
      expect(header.indexOf('left')).toBe(12);
      expect(header.indexOf('busy')).toBe(34);
      expect(header.indexOf('exited')).toBe(54);
      expect(header.indexOf('idle')).toBe(40);
      expect(header.indexOf('total')).toBe(62);
      const firstRows = lines.filter(line => line.startsWith('● '));
      for (const [i, row] of firstRows.entries()) {
        expect(row.indexOf(LABELS[i]!)).toBe(3);
        expect(row.slice(18, 32).trim()).toBe(i === 0 || i === 4 ? 'showrunners'
          : i === 3 || i === 5 ? 'spare' : 'workers');
        expect(row.slice(12, 16).trim()).toBe(['60%', '20%', '8%', '25%', '—', '90%', '74%'][i]);
        expect(row[15]).toBe(i === 4 ? '—' : '%');
        expect(row.slice(34, 38).trim()).toBe(['1', '7', '0', '', '0', '', '1'][i]);
        expect(row.slice(54, 60).trim()).toBe(['1', '0', '0', '', '0', '', '1'][i]);
        expect(row.slice(40, 44).trim()).toBe(['0', '5', '1', '', '0', '', '2'][i]);
        expect(row.slice(62, 67).trim()).toBe(['2', '12', '1', '', '0', '', '4'][i]);
      }
      const detail = columns === 140 ? firstRows : firstRows.map(row => lines[lines.indexOf(row) + 1]!);
      const resetStart = columns === 140 ? 69 : 2;
      const creditStart = columns === 140 ? 80 : 13;
      const enabledStart = columns === 140 ? 91 : 24;
      for (const [i, row] of detail.entries()) {
        expect(row.slice(resetStart, resetStart + 9).trim()).toBe(['14:00', 'Sat 14:00', 'Sun 14:00', '23 Oct', '', 'Thu 14:00', '24 Oct'][i]);
        expect(row.slice(creditStart, creditStart + 9).trim()).toBe(i === 0 ? 'unlimited' : i === 1 ? '13,446' : '');
        expect(row.slice(enabledStart, enabledStart + 7).trim()).toBe(i === 0 || i === 5 ? 'yes' : '');
      }
      await ui.unmount();
    }
  });

  for (const columns of [140, 80, 50]) {
    test(`all columns remain at ${columns} cells when no balance exists`, async ($, on) => {
      const fake = accountsHost(on, [], { USER_ZONE: 'America/New_York' });
      const data = readableFixture();
      data.accounts = data.accounts.map(account => ({ ...account, credits: null }));
      data.problems = [];
      fake.replace(data);
      await $.command.run(command());
      const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane', surface: 'terminal',
        props: { ...props, bodyColumns: columns } });
      const lines = rowLines(await ui.drawn());
      // Empty balances retain every column and the credit setting.
      if (columns === 50) {
        expect(normalized(accountLines(await ui.drawn(), 'aLpha')).replace(/ · /g, ' ')).toBe(
          '● aLpha 60% left role showrunners busy 1 idle 0 paused 0 exited 1 total 2 resets 14:00 enabled role unit directors busy 2 idle 1 paused 0 exited 0 total 3');
      } else {
        expect(lines.slice(0, lines.findIndex(line => line.startsWith('● '))).join(' ').trim().split(/\s+/))
          .toEqual(['account', 'left', 'role', 'busy', 'idle', 'paused', 'exited', 'total', 'resets', 'credits', 'enabled']);
      }
      if (columns === 50) expect(lines.join(' ')).not.toContain('credits');
      else expect(lines.join(' ')).toContain('credits');
      expect(lines.join(' ')).not.toContain('in use');
      expect(lines.filter(line => line.startsWith('● '))).toHaveLength(7);
    });
  }

  test('wide labels use terminal cells and preserve all their characters when wrapping', async ($, on) => {
    const fake = accountsHost(on);
    const label = '用户名'.repeat(8) + ' e\u0301😀';
    const data = statusFixture();
    data.accounts = [{ ...data.accounts[0]!, label }];
    data.problems = [];
    fake.replace(data);
    await $.command.run(command());
    const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane', surface: 'terminal',
      props: { ...props, bodyColumns: 40 } });
    const lines = rowLines(await ui.drawn());
    for (const line of lines) expect(cellWidth(line)).toBeLessThanOrEqual(40);
    expect(lines.join('').replace(/\s/g, '')).toContain(label.replace(/\s/g, ''));
    expect(lines.join('')).not.toContain('…');
  });

  test('access marks are independent of quota and only quota cells use low-quota tones', async ($, on) => {
    const fake = accountsHost(on, [], { USER_ZONE: 'America/New_York' });
    const data = readableFixture();
    data.problems = [];
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    const tree = await ui.drawn();
    expect(textInColor(tree, 'error').replace(/\s/g, '')).toBe('●8%●');
    expect(textInColor(tree, 'warning').replace(/\s/g, '')).toBe('20%');
    expect(textInColor(tree, 'success').replace(/\s/g, '')).toBe('●●●●●');
    expect(textInColor(tree, 'inactive')).not.toContain('●');
    const ordinary = textInColor(tree, 'default');
    for (const fact of ['60%', '25%', '90%', '74%', '—',
      'showrunners', 'unit directors', 'workers', 'spare', 'unlimited', '13,446', '14:00', '24 Oct']) {
      expect(ordinary).toContain(fact);
    }
  });

  test('quota boundaries color the left cell without recoloring accessible marks', async ($, on) => {
    const fake = accountsHost(on);
    const data = statusFixture();
    data.accounts = [9, 10, 24, 25].map(value => ({ ...data.accounts[0]!, label: `Level ${value}`,
      weekly_left_percent: value, credits: null, use_credits: false, credits_in_use: false }));
    data.problems = [];
    fake.replace(data);
    await $.command.run(command());
    const tree = await (await mount($)).drawn();
    expect(textInColor(tree, 'error').replace(/\s/g, '')).toBe('9%');
    expect(textInColor(tree, 'warning').replace(/\s/g, '')).toBe('10%24%');
    expect(textInColor(tree, 'success').replace(/\s/g, '')).toBe('●●●●');
    expect(textInColor(tree, 'default')).toContain('25%');
  });

  for (const [at, expected] of [
    ['2026-10-17T02:00:00Z', '22:00'], // Same New York day, different UTC day.
    ['2026-10-10T18:00:00Z', 'Sat 14:00'],
    ['2026-10-22T18:00:00Z', 'Thu 14:00'],
    ['2026-10-09T18:00:00Z', '9 Oct'],
    ['2026-10-24T18:00:00Z', '24 Oct'],
  ] as const) {
    test(`short reset times in the resolved zone render ${at} as ${expected}`, async ($, on) => {
      const fake = accountsHost(on, [], { USER_ZONE: 'America/New_York' });
      const account = { ...statusFixture().accounts[0]!, resets_at: at };
      fake.replace({ accounts: [account], problems: [] });
      await $.command.run(command());
      const rows = rowLines(await (await mount($)).drawn());
      expect(rows[0]!.trim().split(/\s+/)).toEqual(['account', 'left', 'role', 'busy', 'idle', 'paused', 'exited', 'total', 'resets', 'credits', 'enabled']);
      expect(rows[1]!.split(expected)).toHaveLength(2);
    });
  }

  test('missing and invalid optional cells are blank, zero credits count as a balance', async ($, on) => {
    const fake = accountsHost(on);
    const account = statusFixture().accounts[2]!;
    fake.replace({ accounts: [account,
      { ...account, label: 'Invalid', resets_at: 'not-a-date', read_at: 'not-a-date', use_credits: true },
      { ...account, label: 'Zero', weekly_left_percent: 0, credits: 0 }], problems: [] });
    await $.command.run(command());
    const tree = await (await mount($)).drawn();
    expect(rowLines(tree)).toEqual([
      "   account  left  role  busy  idle  paused  exited  total  resets  credits  enabled",
      "●  Invalid     —                                                            yes",
      "●  Unknown     —",
      "●  Zero       0%                                                         0",
      "   total                   0     0       0       0      0"
]);
    const text = rowLines(tree).join(' ');
    for (const missing of ['no reading', 'null', 'undefined', 'Problems']) expect(text).not.toContain(missing);
  });

  for (const surface of ['terminal', 'desktop'] as const) {
    for (const columns of [140, 100, 60, 30]) {
      test(`${surface}: ${columns} cells keep failed-read problems and the red dot without a read fact`, async ($, on) => {
        const fake = accountsHost(on);
        const problem = 'Watch: usage unavailable; the usage read failed; last read attempted at 10:58';
        fake.replace({ accounts: [{ ...statusFixture().accounts[1]!, read_at: '2026-10-01T10:58:00Z' }],
          problems: [problem] });
        await $.command.run(command());
        const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane', surface,
          props: { ...props, bodyColumns: columns } });
        const tree = await ui.drawn();
        const lines = rowLines(tree);
        expect(textInColor(tree, 'error')).toBe('●');
        expect(normalized(lines.slice(lines.indexOf('Problems') + 1))).toBe(`• ${problem}`);
        expect(normalized(accountLines(tree, 'Watch'))).not.toMatch(/\bread\b|\bago\b/);
        expect(lines.slice(0, lines.indexOf('Problems')).join(' ')).not.toMatch(/\bread\b/);
        for (const line of lines) expect(cellWidth(line)).toBeLessThanOrEqual(columns);
      });
    }
  }

  test('display projection keeps access and credit settings without spending presentation', () => {
    const enabled = accountDisplay(readableFixture().accounts.find(account => account.label === 'Foxtrot')!);
    expect([enabled.readOk, enabled.useCredits]).toEqual([true, true]);
    const spending = accountDisplay(readableFixture().accounts.find(account => account.label === 'aLpha')!);
    expect([spending.readOk, spending.useCredits]).toEqual([true, true]);
  });

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: older JSON without either signal keeps a red mark and no spending fact`, async ($, on) => {
      const fake = accountsHost(on);
      const { read_ok: _access, credits_in_use: _spending, role_counts: _counts, ...legacy } = statusFixture().accounts[0]!;
      // Deliberately exercise the untyped process-JSON boundary of an older producer.
      fake.replace({ accounts: [{ ...legacy, weekly_left_percent: 80 }], problems: [] } as unknown as StoredAccountsStatus);
      await $.command.run(command());
      const tree = await (await mount($, surface)).drawn();
      expect(textInColor(tree, 'error').replace(/\s/g, '')).toBe('●');
      expect(textInColor(tree, 'success').trim()).toBe('');
      expect(textInColor(tree, 'warning').trim()).toBe('');
      const lines = rowLines(tree);
      expect(lines.join(' ')).not.toContain('in use');
      const display = accountDisplay(legacy as StoredAccountsStatus['accounts'][number]);
      expect(display.readOk).toBe(false);
      expect('creditsInUse' in display).toBe(false);
      expect(display.roleCounts).toEqual([{ kind: 'unavailable' }, { kind: 'unavailable' }]);
      for (const heading of ['busy', 'idle', 'paused', 'exited', 'total']) {
        const start = lines[0]!.indexOf(heading);
        expect(start).toBeGreaterThan(0);
        expect(lines[1]!.slice(start, start + heading.length).trim()).toBe('');
      }
    });
  }

  for (const columns of [140, 50, 30]) {
    test(`${columns} cells: worker roles have separate activity counts and zero exited counts`, async ($, on) => {
      const fake = accountsHost(on);
      fake.replace({ accounts: [{ ...statusFixture().accounts[1]!, roles: ['workers', 'reviewers'],
        role_counts: [{ busy: 2, idle: 3, paused: 0, exited: 0, total: 5 },
          { busy: 1, idle: 4, paused: 0, exited: 0, total: 5 }] }], problems: [] });
      await $.command.run(command());
      const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts', component: 'Pane',
        surface: 'terminal', props: { ...props, bodyColumns: columns } });
      const lines = rowLines(await ui.drawn());
      const row = normalized(accountLines(await ui.drawn(), 'Watch'));
      if (columns === 140) {
        expect(row).toContain('workers 2 3 0 0 5');
        expect(row).toContain('reviewers 1 4 0 0 5');
      } else {
        const facts = row.replace(/ · /g, ' ');
        expect(facts).toContain('role workers busy 2 idle 3 paused 0 exited 0 total 5');
        expect(facts).toContain('role reviewers busy 1 idle 4 paused 0 exited 0 total 5');
      }
      expect(lines.join(' ')).not.toContain('in use');
      if (columns === 140) {
        const exited = lines[0]!.indexOf('exited');
        expect(lines[1]!.slice(exited, exited + 6).trim()).toBe('0');
        expect(lines.at(-1)!.slice(exited, exited + 6).trim()).toBe('0');
        expect(lines.at(-1)!.trim().replace(/\s+/g, ' ')).toBe('total 3 7 0 0 10');
      } else {
        const total = lines.indexOf('total');
        expect(total).toBeGreaterThan(0);
        expect(normalized(lines.slice(total))).toBe(columns === 30 ? 'total busy 3 · idle 7 · paused 0 exited 0 · total 10' : 'total busy 3 · idle 7 · paused 0 · exited 0 · total 10');
        expect(row).toContain('exited 0');
      }
      for (const line of lines) expect(cellWidth(line)).toBeLessThanOrEqual(columns);
    });
  }

  test('the display projection drops a legacy login address', () => {
    const account = legacyLoginFixture().accounts[0]!;
    const display = accountDisplay(account);
    expect('login' in display).toBe(false);
    expect(JSON.stringify(display).includes(account.login)).toBe(false);
  });
});

describe('accounts pane', () => {
  test('local session inputs refresh role counts on every tick without a usage change or extra discovery', async ($, on) => {
    const fake = accountsHost(on);
    const path = `${HOME}/.claude/sessions/unit-a.json`;
    const data = statusFixture();
    data.session_inputs = [path];
    fake.mtimes.set(path, NOW - 1000);
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 1 0 1 3');
    fake.clear();
    const changed = statusFixture();
    changed.session_inputs = [path];
    changed.accounts[0]!.role_counts = [{ busy: 1, idle: 2, paused: 0, exited: 1, total: 4 }, { busy: 1, idle: 1, paused: 0, exited: 0, total: 2 }];
    fake.replace(changed);
    fake.change(path);
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 2 0 1 4');
    expect(fake.otherRuns()).toEqual([]);
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(fake.escaped).toEqual([]);
    expect(hasSensitivePath(fake.fs)).toBe(false);
  });

  test('a killed owner changes counts on the next tick even when every watched mtime stays unchanged', async ($, on) => {
    const fake = accountsHost(on);
    const path = `${HOME}/.claude/sessions/killed-owner.json`;
    const data = statusFixture();
    data.session_inputs = [path];
    fake.mtimes.set(path, NOW - 1000);
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 1 0 1 3');
    const unchanged = [...fake.mtimes.entries()];
    const afterExit = structuredClone(data);
    afterExit.accounts[0]!.role_counts = [{ busy: 1, idle: 1, paused: 0, exited: 0, total: 2 }, { busy: 1, idle: 1, paused: 0, exited: 0, total: 2 }];
    fake.replace(afterExit);
    fake.clear();
    await fake.clock.advance(10_000);
    expect([...fake.mtimes.entries()]).toEqual(unchanged);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(fake.otherRuns()).toEqual([]);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 1 0 0 2');
    expect(fake.escaped).toEqual([]);
    expect(fake.forbidden).toEqual([]);
  });

  test('a failed liveness refresh retries on the next unchanged tick', async ($, on) => {
    const fake = accountsHost(on);
    const path = `${HOME}/.claude/sessions/owner.json`;
    const data = statusFixture();
    data.session_inputs = [path];
    fake.mtimes.set(path, NOW - 1000);
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 1 0 1 3');
    const unchanged = [...fake.mtimes.entries()];
    fake.clear();
    fake.failNext();
    await fake.clock.advance(10_000);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(fake.accountStateKind()).toBe('failed');
    const recovered = structuredClone(data);
    recovered.accounts[0]!.role_counts = [{ busy: 1, idle: 1, paused: 0, exited: 0, total: 2 }, { busy: 1, idle: 1, paused: 0, exited: 0, total: 2 }];
    fake.replace(recovered);
    fake.clear();
    await fake.clock.advance(10_000);
    expect([...fake.mtimes.entries()]).toEqual(unchanged);
    expect(fake.accountRuns()).toEqual([ARGV]);
    expect(fake.accountStateKind()).toBe('accounts');
    expect(normalized(accountLines(await ui.drawn(), 'Low'))).toContain('unit directors 1 1 0 0 2');
    expect(fake.otherRuns()).toEqual([]);
    expect(fake.escaped).toEqual([]);
    expect(fake.forbidden).toEqual([]);
  });

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: stored status opens private account lines with roles and problems`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      const result = await $.command.run(command());
      expect(result.text === 'Accounts opened.').toBe(true);
      expect(result.text?.includes('@') ?? false).toBe(false);
      expect(fake.accountRuns()).toEqual([ARGV]);
      expect(fake.opened).toEqual(['accounts']);
      const ui = await mount($, surface);
      const text = drawnText(await ui.drawn());
      for (const word of ['●', 'Left', 'Resets', 'Credits', 'Read', 'Problems', 'Low', 'Watch', 'Unknown', 'showrunners', 'workers',
        'Stored reading unavailable for one account.', 'unlimited', '42']) {
        expect(text.toLowerCase().includes(word.toLowerCase())).toBe(true);
      }
      for (const heading of OLD_HEADINGS) expect(text.includes(heading)).toBe(false);
      expect(text.includes('@')).toBe(false);
      expect(text.includes('unit directors, showrunners')).toBe(false);
      expect(text.includes('unit directors')).toBe(true);
      expect(hasSensitivePath(fake.fs)).toBe(false);
      expect(fake.escaped.length).toBe(0);
      expect(fake.forbidden).toEqual([]);
    });
  }

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: a legacy login field draws no address on open or refresh`, async ($, on) => {
      const fake = accountsHost(on);
      fake.replace(legacyLoginFixture());
      await $.session.start(start);
      await $.command.run(command());
      expect(fake.accountRuns()).toEqual([ARGV]);
      const ui = await mount($, surface);
      let text = drawnText(await ui.drawn());
      expect(text.includes('Low')).toBe(true);
      expect(text.includes('@')).toBe(false);
      expect(text.includes('Login')).toBe(false);
      const changed = legacyLoginFixture();
      changed.accounts[0]!.label = 'Refreshed';
      changed.accounts[0]!.login = address('changed');
      fake.replace(changed);
      fake.change(USAGE);
      await fake.clock.advance(10_000);
      expect(fake.accountRuns()).toEqual([ARGV, ARGV]);
      text = drawnText(await ui.drawn());
      expect(text.includes('Refreshed')).toBe(true);
      expect(text.includes('@')).toBe(false);
      expect(text.includes('Login')).toBe(false);
      expect(fake.escaped).toEqual([]);
    });
  }

  for (const config of ['repository', 'override']) {
    test(`${config} zone configuration formats resets when the environment zone is unset`, async ($, on) => {
      const environment = config === 'override' ? { USER_ZONE_CONF: '/test-zone.conf' } : {};
      const fake = accountsHost(on, [], environment);
      await $.session.start(start);
      await $.command.run(command());
      const ui = await mount($);
      const text = drawnText(await ui.drawn());
      expect(text.includes('03:00')).toBe(true);
      expect(text).not.toMatch(/\bread\b|\bago\b/);
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
    expect(text).not.toMatch(/\bread\b|\bago\b/);
  });

  test('an environment zone overrides the stored zone without reading its file', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    const ui = await mount($);
    const text = drawnText(await ui.drawn());
    expect(text.includes('18:00')).toBe(true);
    expect(text).not.toMatch(/\bread\b|\bago\b/);
    expect(fake.fs.includes(`${HOME}/.claude/config/user_zone.conf`)).toBe(false);
  });

  for (const role of ['units', 'unit directors']) {
    test(`${role} assignments stay beside their account labels`, async ($, on) => {
      const fake = accountsHost(on);
      const data = statusFixture();
      data.accounts[0]!.roles = [role];
      data.accounts[1]!.roles = ['showrunners'];
      fake.replace(data);
      await $.session.start(start);
      await $.command.run(command());
      const ui = await mount($);
      const tree = await ui.drawn();
      const lines = rowLines(tree);
      expect(lines[1]).toContain('●  Low');
      expect(lines.some(line => /^\s*units:/i.test(line))).toBe(false);
      expect(normalized(accountLines(tree, 'Low')).includes(role)).toBe(true);
      expect(normalized(accountLines(tree, 'Watch')).includes('showrunners')).toBe(true);
    });
  }

  test('closing accounts acknowledges closure without an address', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    const result = await $.command.run(command('showrunner:accounts', 'close'));
    expect(result.text === 'Accounts closed.').toBe(true);
    expect(result.text?.includes('@') ?? false).toBe(false);
    expect(fake.closed).toEqual(['accounts']);
  });

  test('closing during startup zone lookup cannot restart accounts', async ($, on) => {
    const fake = accountsHost(on, [], {});
    await $.session.start(start);
    await $.command.run(command());
    const pending = fake.pauseZone();
    const restarting = $.session.start(start);
    await pending.started;
    await $.command.run(command('showrunner:accounts', 'close'));
    fake.clear();
    pending.finish();
    await restarting;
    await fake.clock.advance(60_000);
    expect(fake.accountStateKind()).toBe('closed');
    expect(fake.stateWrites).toEqual([]);
    expect(fake.accountRuns()).toEqual([]);
    expect(fake.fs.some(path => INPUTS.includes(path))).toBe(false);
    expect(fake.fs.includes(`${HOME}/.claude/config/user_zone.conf`)).toBe(false);
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
      ui: { panes: async () => [{ id: 'accounts' }] },
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
    await dispatch('ui.close', { id: 'accounts', origin: { kind: 'person' } });
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
    expect(fake.stateWrites.every(kind => kind === 'accounts')).toBe(true);
    expect(fake.fs.includes(`${HOME}/.local/state/accounts`)).toBe(false);
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
    await $.command.run(command('showrunner:accounts', 'close'));
    fake.clear();
    await fake.clock.advance(60_000);
    await $.turn.complete(turn);
    expect(fake.fs).toEqual([]);
    expect(fake.processes).toEqual([]);
  });

  for (const close of ['showrunner:accounts', 'build:queue']) {
    test(`closing ${close} preserves the accounts lifecycle`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      await $.command.run(command());
      await $.command.run(command(close, 'close'));
      fake.clear();
      fake.change(USAGE);
      await fake.clock.advance(10_000);
      expect(fake.accountRuns().length).toBe(close === 'showrunner:accounts' ? 0 : 1);
      expect(fake.fs.includes(USAGE)).toBe(close !== 'showrunner:accounts');
      await $.command.run(command(close === 'showrunner:accounts' ? 'build:queue' : 'showrunner:accounts', 'close'));
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
      expect(text.includes('left')).toBe(true);
      expect(text.includes('9%')).toBe(true);
      for (const heading of OLD_HEADINGS) expect(text.includes(heading)).toBe(false);
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
    expect(fake.opened).toEqual(['accounts']);
  });

  for (const refresh of [false, true]) {
    test(`closing during pending ${refresh ? 'refresh' : 'open'} leaves no state or background work`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
        if (refresh) await $.command.run(command());
      const pending = fake.pause();
      let opening: Promise<unknown> = Promise.resolve();
      if (refresh) { fake.change(USAGE); opening = fake.clock.advance(10_000); }
      else opening = $.command.run(command());
      await pending.started;
      await $.command.run(command('showrunner:accounts', 'close'));
      fake.clear();
      pending.finish();
      await opening;
      await fake.clock.settle();
      expect(fake.accountStateKind()).toBe('closed');
      expect(fake.stateWrites.length).toBe(0);
      expect(fake.fs).toEqual([]);
      await fake.clock.advance(10_000);
      expect(fake.accountRuns()).toEqual([]);
      expect(fake.otherRuns()).toEqual([]);
    });
  }
});
