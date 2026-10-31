import { describe, expect, mock, test } from 'claude-code/testing';
import type { Engine } from 'claude-code/testing';
import type { CommandRunInput, EngineInterface, On, TurnCompleteInput } from 'claude-code';
import { register } from '../hooks/register';
import { accountDisplay } from '../hooks/accounts';
import { cellWidth } from '../hooks/geometry';
import type { StoredAccountsStatus } from '../types/accounts';
import { BOARD, CWD, DISCOVERY, drawnText, element, elements, fixture, hostEngine, NOW, REGISTRY, rowLines, RUN, styledText } from './kit';

const HOME = '/test-home';
const USAGE = `${HOME}/.local/state/agent-notes/usage.json`;
const GROUPS = `${HOME}/.local/state/accounts/groups.json`;
const CACHE = `${HOME}/.local/state/accounts/role_map_cache.json`;
const CONFIG = `${HOME}/.claude/config/accounts.json`;
const INPUTS = [USAGE, GROUPS, CACHE, CONFIG];
const ARGV = [`${HOME}/.claude/scripts/lib/py`, `${HOME}/.claude/scripts/accounts/accounts.py`,
  'status', '--json', '--stored'];
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
    { label: 'Low', tool: 'claude', roles: ['unit directors', 'showrunners'],
      weekly_left_percent: 9, resets_at: '2026-10-18T18:00:00+00:00', credits: 42,
      use_credits: true, read_ok: true, credits_in_use: true, read_at: '2026-10-16T17:59:00+00:00' },
    { label: 'Watch', tool: 'codex', roles: ['workers'],
      weekly_left_percent: 24, resets_at: null, credits: 'unlimited', use_credits: false, read_ok: false, credits_in_use: false, read_at: null },
    { label: 'Unknown', tool: 'claude', roles: [], weekly_left_percent: null,
      resets_at: null, credits: null, use_credits: false, read_ok: false, credits_in_use: false, read_at: null },
  ], problems: ['Stored reading unavailable for one account.'] };
}
const OLD_HEADINGS = ['Login', 'Weekly left', 'Weekly resets', 'On credits', 'Credits left'];
/** Four Claude and three Codex accounts; deliberately unsorted and mixed case. */
function readableFixture(): StoredAccountsStatus {
  return { accounts: [
    { label: 'Gamma', tool: 'claude', roles: ['workers'], weekly_left_percent: 74,
      resets_at: '2026-10-24T18:00:00Z', credits: null, use_credits: false, read_ok: true, credits_in_use: false,
      read_at: '2026-10-16T17:55:00Z' },
    { label: 'beta', tool: 'codex', roles: ['workers'], weekly_left_percent: 20.2,
      resets_at: '2026-10-17T18:00:00Z', credits: 13446.2, use_credits: false, read_ok: false, credits_in_use: false,
      read_at: '2026-10-16T17:48:00Z' },
    { label: 'Echo', tool: 'claude', roles: ['showrunners', 'unit directors'], weekly_left_percent: null,
      resets_at: null, credits: null, use_credits: false, read_ok: false, credits_in_use: false, read_at: null },
    { label: 'aLpha', tool: 'claude', roles: ['showrunners', 'unit directors'], weekly_left_percent: 60.4,
      resets_at: '2026-10-16T18:00:00Z', credits: 'unlimited', use_credits: true, read_ok: true, credits_in_use: true,
      read_at: '2026-10-16T17:59:15Z' },
    { label: 'Foxtrot', tool: 'codex', roles: ['spare'], weekly_left_percent: 90,
      resets_at: '2026-10-22T18:00:00Z', credits: null, use_credits: true, read_ok: true, credits_in_use: false,
      read_at: '2026-10-16T17:56:00Z' },
    { label: 'Charlie', tool: 'claude', roles: ['workers'], weekly_left_percent: 8.4,
      resets_at: '2026-10-18T18:00:00Z', credits: null, use_credits: false, read_ok: true, credits_in_use: false,
      read_at: '2026-10-16T16:00:00Z' },
    { label: 'Delta', tool: 'codex', roles: ['spare'], weekly_left_percent: 25,
      resets_at: '2026-10-23T18:00:00Z', credits: null, use_credits: false, read_ok: true, credits_in_use: false,
      read_at: '2026-10-13T18:00:00Z' },
  ], problems: [
    'Stored weekly reading is unavailable for the spare account; keep its previous assignment until another reading arrives.',
    'The role cache has an old showrunner assignment that needs refreshing before the next production starts.',
  ] };
}
// Literal cells from the phase 20 shared-table spec; no renderer-derived expectations.
const TABLE_DRAWN_LINES: Record<number, readonly string[]> = {
  140: [
    '   account  role                         left     resets    credits  enabled  in use  read',
    '●  aLpha    showrunners, unit directors   60%      14:00  unlimited  yes      yes      45s',
    '●  beta     workers                       20%  Sat 14:00     13,446                    12m',
    '●  Charlie  workers                        8%  Sun 14:00                                2h',
    '●  Delta    spare                         25%     23 Oct                                3d',
    '●  Echo     showrunners, unit directors     —',
    '●  Foxtrot  spare                         90%  Thu 14:00             yes                4m',
    '●  Gamma    workers                       74%     24 Oct                                5m',
    '',
    'Problems',
    '• Stored weekly reading is unavailable for the spare account; keep its previous assignment until another reading arrives.',
    '• The role cache has an old showrunner assignment that needs refreshing before the next production starts.',
  ],
  60: [
    '   account  role                         left',
    '     resets    credits  enabled  in use  read',
    '●  aLpha    showrunners, unit directors   60%',
    '      14:00  unlimited  yes      yes      45s',
    '●  beta     workers                       20%',
    '  Sat 14:00     13,446                    12m',
    '●  Charlie  workers                        8%',
    '  Sun 14:00                                2h',
    '●  Delta    spare                         25%',
    '     23 Oct                                3d',
    '●  Echo     showrunners, unit directors     —',
    '',
    '●  Foxtrot  spare                         90%',
    '  Thu 14:00             yes                4m',
    '●  Gamma    workers                       74%',
    '     24 Oct                                5m',
    '',
    'Problems',
    '• Stored weekly reading is unavailable for the spare',
    '  account; keep its previous assignment until another',
    '  reading arrives.',
    '• The role cache has an old showrunner assignment that',
    '  needs refreshing before the next production starts.',
  ],
  30: [
    '● aLpha  showrunners, unit',
    '  directors',
    '  60% left · resets 14:00',
    '  credits unlimited · enabled',
    '  in use · read 45s ago',
    '● beta  workers',
    '  20% left · resets Sat 14:00',
    '  credits 13,446',
    '  read 12m ago',
    '● Charlie  workers',
    '  8% left · resets Sun 14:00',
    '  read 2h ago',
    '● Delta  spare',
    '  25% left · resets 23 Oct',
    '  read 3d ago',
    '● Echo  showrunners, unit',
    '  directors',
    '  — left',
    '● Foxtrot  spare',
    '  90% left · resets Thu 14:00',
    '  enabled · read 4m ago',
    '● Gamma  workers',
    '  74% left · resets 24 Oct',
    '  read 5m ago',
    '',
    'Problems',
    '• Stored weekly reading is',
    '  unavailable for the spare',
    '  account; keep its previous',
    '  assignment until another',
    '  reading arrives.',
    '• The role cache has an old',
    '  showrunner assignment that',
    '  needs refreshing before the',
    '  next production starts.',
  ],
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
  const next = rest.findIndex(line => line.startsWith('● ') || line === '' || line === 'Problems');
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
    return { value: { exitCode: 0, stdout: JSON.stringify({ files: [BOARD], watch: [REGISTRY] }),
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
describe('accounts table', () => {
  for (const surface of ['terminal', 'desktop'] as const) {
    for (const columns of [140, 60, 30]) {
      test(`${surface}: exact table form at ${columns} cells keeps every fact and problem`, async ($, on) => {
        const fake = accountsHost(on, [], { USER_ZONE: 'America/New_York' });
        const data = readableFixture();
        const before = JSON.stringify(data);
        fake.replace(data);
        await $.command.run(command());
        const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts-board', component: 'Pane', surface,
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
        for (const heading of ['account', 'role', 'left', 'resets', 'credits', 'enabled', 'in use', 'read']) {
          const headings = spans.filter(span => span.bold && span.text.trim() === heading);
          expect(headings.length).toBe(columns === 30 ? 0 : 1);
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
    for (const columns of [140, 60]) {
      const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts-board', component: 'Pane', surface: 'terminal',
        props: { ...props, bodyColumns: columns } });
      const lines = rowLines(await ui.drawn());
      const header = lines[0]!;
      expect(header.indexOf('account')).toBe(3);
      expect(header.indexOf('role')).toBe(12);
      expect(header.indexOf('left')).toBe(41);
      const firstRows = lines.filter(line => line.startsWith('● '));
      for (const [i, row] of firstRows.entries()) {
        expect(row.indexOf(LABELS[i]!)).toBe(3);
        expect(row.slice(12, 39).trim()).toBe(i === 0 || i === 4 ? 'showrunners, unit directors'
          : i === 3 || i === 5 ? 'spare' : 'workers');
        expect(row.slice(41, 45).trim()).toBe(['60%', '20%', '8%', '25%', '—', '90%', '74%'][i]);
        expect(row[44]).toBe(i === 4 ? '—' : '%');
      }
      const detail = columns === 140 ? firstRows : LABELS.map((_, i) => lines[3 + i * 2]!);
      const resetStart = columns === 140 ? 47 : 2;
      const creditStart = columns === 140 ? 58 : 13;
      const enabledStart = columns === 140 ? 69 : 24;
      const inUseStart = columns === 140 ? 78 : 33;
      const readStart = columns === 140 ? 86 : 41;
      for (const [i, row] of detail.entries()) {
        expect(row.slice(resetStart, resetStart + 9).trim()).toBe(['14:00', 'Sat 14:00', 'Sun 14:00', '23 Oct', '', 'Thu 14:00', '24 Oct'][i]);
        expect(row.slice(creditStart, creditStart + 9).trim()).toBe(i === 0 ? 'unlimited' : i === 1 ? '13,446' : '');
        expect(row.slice(enabledStart, enabledStart + 7).trim()).toBe(i === 0 || i === 5 ? 'yes' : '');
        expect(row.slice(inUseStart, inUseStart + 6).trim()).toBe(i === 0 ? 'yes' : '');
        expect(row.slice(readStart, readStart + 4).trim()).toBe(['45s', '12m', '2h', '3d', '', '4m', '5m'][i]);
      }
      await ui.unmount();
    }
  });

  for (const columns of [140, 60]) {
    test(`all columns remain at ${columns} cells when no balance exists`, async ($, on) => {
      const fake = accountsHost(on, [], { USER_ZONE: 'America/New_York' });
      const data = readableFixture();
      data.accounts = data.accounts.map(account => ({ ...account, credits: null }));
      data.problems = [];
      fake.replace(data);
      await $.command.run(command());
      const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts-board', component: 'Pane', surface: 'terminal',
        props: { ...props, bodyColumns: columns } });
      const lines = rowLines(await ui.drawn());
      // Empty balances do not remove a column or hide the independent switches.
      expect(lines.slice(0, columns === 140 ? 1 : 2).join(' ').trim().split(/\s+/))
        .toEqual(['account', 'role', 'left', 'resets', 'credits', 'enabled', 'in', 'use', 'read']);
      expect(lines.join(' ')).toContain('credits');
      expect(lines.join(' ')).toContain('in use');
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
    const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts-board', component: 'Pane', surface: 'terminal',
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
    expect(textInColor(tree, 'warning').replace(/\s/g, '')).toBe('yes20%');
    expect(textInColor(tree, 'success').replace(/\s/g, '')).toBe('●●●●●');
    expect(textInColor(tree, 'inactive')).not.toContain('●');
    const ordinary = textInColor(tree, 'default');
    for (const fact of ['60%', '25%', '90%', '74%', '—',
      'showrunners, unit directors', 'workers', 'spare', 'unlimited', '13,446', '14:00', '45s', '24 Oct']) {
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
      expect(rows[0]!.trim().split(/\s+/)).toEqual(['account', 'role', 'left', 'resets', 'credits', 'enabled', 'in', 'use', 'read']);
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
      '   account  role  left  resets  credits  enabled  in use  read',
      '●  Invalid           —                   yes',
      '●  Unknown           —',
      '●  Zero             0%                0',
    ]);
    const text = rowLines(tree).join(' ');
    for (const missing of ['no reading', 'null', 'undefined', 'Problems']) expect(text).not.toContain(missing);
  });

  for (const columns of [140, 60, 30]) {
    for (const [ageSeconds, expected] of [
      [45.9, '45s'], [59.999, '59s'], [60, '1m'], [779.9, '12m'],
      [3599.9, '59m'], [3600, '1h'], [10799.9, '2h'],
      [86399.9, '23h'], [86400, '1d'], [345599.9, '3d'],
    ] as const) {
      test(`${columns} cells: read age floors ${ageSeconds}s to ${expected}`, async ($, on) => {
        const fake = accountsHost(on);
        const account = { ...statusFixture().accounts[0]!,
          read_at: new Date(NOW - ageSeconds * 1000).toISOString() };
        fake.replace({ accounts: [account], problems: [] });
        await $.command.run(command());
        const ui = await $.ui.mount({ plugin: 'unit-board', requestId: 'accounts-board', component: 'Pane',
          surface: 'terminal', props: { ...props, bodyColumns: columns } });
        const lines = rowLines(await ui.drawn());
        if (columns === 30) expect(normalized(lines)).toContain(`read ${expected} ago`);
        else expect(lines.at(-1)?.trim().split(/\s+/).at(-1)).toBe(expected);
        for (const line of lines) expect(cellWidth(line)).toBeLessThanOrEqual(columns);
      });
    }
  }

  test('display projection carries independent access and spending booleans', () => {
    const enabled = accountDisplay(readableFixture().accounts.find(account => account.label === 'Foxtrot')!);
    expect([enabled.readOk, enabled.creditsInUse, enabled.useCredits]).toEqual([true, false, true]);
    const spending = accountDisplay(readableFixture().accounts.find(account => account.label === 'aLpha')!);
    expect([spending.readOk, spending.creditsInUse, spending.useCredits]).toEqual([true, true, true]);
  });

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: older JSON without either signal keeps a red mark and blank in use`, async ($, on) => {
      const fake = accountsHost(on);
      const { read_ok: _access, credits_in_use: _spending, ...legacy } = statusFixture().accounts[0]!;
      // Deliberately exercise the untyped process-JSON boundary of an older producer.
      fake.replace({ accounts: [{ ...legacy, weekly_left_percent: 80 }], problems: [] } as unknown as StoredAccountsStatus);
      await $.command.run(command());
      const tree = await (await mount($, surface)).drawn();
      expect(textInColor(tree, 'error').replace(/\s/g, '')).toBe('●');
      expect(textInColor(tree, 'success').trim()).toBe('');
      expect(textInColor(tree, 'warning').trim()).toBe('');
      const lines = rowLines(tree);
      const inUse = lines[0]!.indexOf('in use');
      expect(inUse).toBeGreaterThan(0);
      expect(lines[1]!.slice(inUse, inUse + 6).trim()).toBe('');
      const display = accountDisplay(legacy as StoredAccountsStatus['accounts'][number]);
      expect([display.readOk, display.creditsInUse]).toEqual([false, false]);
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
  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: stored status opens private account lines with roles and problems`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      const result = await $.command.run(command());
      expect(result.text === 'Accounts opened.').toBe(true);
      expect(result.text?.includes('@') ?? false).toBe(false);
      expect(fake.accountRuns()).toEqual([ARGV]);
      expect(fake.opened).toEqual(['accounts-board']);
      const ui = await mount($, surface);
      const text = drawnText(await ui.drawn());
      for (const word of ['●', 'Left', 'Resets', 'Credits', 'Read', 'Problems', 'Low', 'Watch', 'Unknown', 'showrunners', 'workers',
        'Stored reading unavailable for one account.', 'unlimited', '42']) {
        expect(text.toLowerCase().includes(word.toLowerCase())).toBe(true);
      }
      for (const heading of OLD_HEADINGS) expect(text.includes(heading)).toBe(false);
      expect(text.includes('@')).toBe(false);
      expect(text.includes('unit directors, showrunners')).toBe(true);
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
    test(`${config} zone configuration formats resets and readings when the environment zone is unset`, async ($, on) => {
      const environment = config === 'override' ? { USER_ZONE_CONF: '/test-zone.conf' } : {};
      const fake = accountsHost(on, [], environment);
      await $.session.start(start);
      await $.command.run(command());
      const ui = await mount($);
      const text = drawnText(await ui.drawn());
      expect(text.includes('03:00')).toBe(true);
      expect(text.includes('1m')).toBe(true);
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
    expect(text.includes('1m')).toBe(true);
  });

  test('an environment zone overrides the stored zone without reading its file', async ($, on) => {
    const fake = accountsHost(on);
    await $.session.start(start);
    await $.command.run(command());
    const ui = await mount($);
    const text = drawnText(await ui.drawn());
    expect(text.includes('18:00')).toBe(true);
    expect(text.includes('1m')).toBe(true);
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
    const result = await $.command.run(command('accounts-board', 'close'));
    expect(result.text === 'Accounts closed.').toBe(true);
    expect(result.text?.includes('@') ?? false).toBe(false);
    expect(fake.closed).toEqual(['accounts-board']);
  });

  test('closing during startup zone lookup cannot restart accounts while the board keeps refreshing', async ($, on) => {
    const fake = accountsHost(on, [], {});
    await $.session.start(start);
    await $.command.run(command('showrunner:unit_board'));
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
    await $.command.run(command('accounts-board', 'close'));
    fake.clear();
    await fake.clock.advance(60_000);
    await $.turn.complete(turn);
    expect(fake.fs).toEqual([]);
    expect(fake.processes).toEqual([]);
  });

  for (const close of ['accounts-board', 'showrunner:unit_board']) {
    test(`closing ${close} preserves the other pane refresh loop`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      await $.command.run(command('showrunner:unit_board'));
      await $.command.run(command());
      await $.command.run(command(close, 'close'));
      fake.clear();
      fake.change(USAGE);
      fake.change(RUN);
      await fake.clock.advance(10_000);
      expect(fake.accountRuns().length).toBe(close === 'accounts-board' ? 0 : 1);
      expect(fake.boardRuns().length).toBe(close === 'showrunner:unit_board' ? 0 : 1);
      expect(fake.fs.includes(USAGE)).toBe(close !== 'accounts-board');
      expect(fake.fs.includes(RUN)).toBe(close !== 'showrunner:unit_board');
      await $.command.run(command(close === 'accounts-board' ? 'showrunner:unit_board' : 'accounts-board', 'close'));
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
    expect(fake.opened).toEqual(['accounts-board']);
  });

  for (const refresh of [false, true]) {
    test(`closing during pending ${refresh ? 'refresh' : 'open'} leaves no state and keeps the board live`, async ($, on) => {
      const fake = accountsHost(on);
      await $.session.start(start);
      await $.command.run(command('showrunner:unit_board'));
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
