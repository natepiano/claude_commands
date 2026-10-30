import { describe, expect, mock, test } from 'claude-code/testing';
import type { Engine } from 'claude-code/testing';
import type { CommandRunInput, EngineInterface, On, TurnCompleteInput, UiPane } from 'claude-code';
import { register } from '../hooks/register';
import { queueLines } from '../hooks/queue';
import { cellWidth } from '../hooks/geometry';
import type { QueuePaneState, QueueSnapshot } from '../types/queue';
import type { AccountsPaneState } from '../types';
import type { StoredAccountsStatus } from '../types/accounts';
import { CWD, drawnText, NOW } from './kit';

const HOME = '/test-home';
const ARGV = [`${HOME}/.claude/scripts/lib/py`, `${HOME}/.claude/scripts/build_queue/build_queue.py`];
const command = (args = ''): CommandRunInput => ({ command: 'build:queue', args,
  origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 170 } });
const start = { cwd: CWD, surface: null, isInteractive: false } as const;
const turn: TurnCompleteInput = { answer: 'finished', reason: 'answer', durationMs: 100,
  isAborted: false, turnId: 'queue-turn' };
const HEAD = 'Stalled: report check: builds use 17.6 of the 20.0 GiB budget; '
  + 'widget-enhancements-title nextest: charged 10.8 GiB, using 0.5 GiB';
const BUMP = 'tester moved report: check in /tmp/report to the head; memory limits still apply';

function snapshot(): QueueSnapshot {
  return {
    running_header: ['#', 'Showrunner', 'Unit', 'Session', 'Step', 'Folder', 'Promised', 'Using', 'Delta', 'State'],
    running: [['1', 'widgets', 'title', 'widget-enhancements-title', 'nextest', 'title-tree',
      '10.8 GiB', '0.5 GiB', '10.3 GiB', 'measured']],
    waiting_header: ['#', 'Showrunner', 'Unit', 'Session', 'Step', 'Folder', 'Waiting since', 'Needs', 'Free', 'Reason'],
    waiting: [['1', 'builds', 'report', 'report', 'check', 'report-tree', '14:45 (15 min)',
      '12.0 GiB', '30.0 GiB', 'builds use 17.6 of the 20.0 GiB budget']],
    head: { text: HEAD, tone: 'error' },
    totals: { promised: '10.8 GiB', measured: '0.5 GiB', free: '30.0 GiB', reserve: '3.0 GiB' },
    problems: ['one session unreadable', 'another machine unavailable'],
  };
}

function queueHost(on: On) {
  const panes = new Map<string, UiPane>();
  const opened: string[] = [];
  const closed: string[] = [];
  const clock = mock.clock(on, { now: NOW });
  mock.env(on, { HOME, USER_ZONE: 'UTC' });
  const processes: string[][] = [];
  const writes: QueuePaneState[] = [];
  const escaped: string[] = [];
  const opens: { id: string; title?: string; closeOnEscape?: boolean }[] = [];
  let data = snapshot();
  let failure: 'none' | 'exit' | 'json' | 'shape' | 'truncated' | 'throw' = 'none';
  let bumpFailure = false;
  let bumpRefusal = '';
  let pending = Promise.resolve();
  let signal = () => {};
  let release = () => {};
  on('session.start', (_$, e) => ({ cwd: e.cwd }));
  on('turn.complete', (_$, e) => ({ text: e.answer }));
  on('command.register', (_$, e) => ({ value: { command: e.name } }));
  on('command.run', (_$, e) => { escaped.push(e.command); return { text: 'Reached the model.' }; });
  on('ui.panes', () => ({ value: [...panes.values()] }));
  on('ui.open', (_$, e) => {
    opens.push(e);
    opened.push(e.id);
    panes.set(e.id, { id: e.id, title: e.title ?? e.id, isShown: true, isFocused: true, isPlaced: true });
    return { value: { isPlaced: true } };
  });
  on('ui.close', (_$, e) => {
    closed.push(e.id);
    panes.delete(e.id);
    return { value: undefined };
  });
  on('state.set', { plugin: 'unit-board', key: 'queuePanes' }, (_$, e, next) => {
    const value = e.value['build-queue'];
    if (value) writes.push(value);
    return next(e);
  });
  on('process.run', async (_$, e) => {
    processes.push([...e.argv]);
    if (e.argv.includes('bump')) {
      return { value: { exitCode: bumpFailure ? 2 : bumpRefusal ? 3 : 0,
        stdout: bumpFailure ? 'PRIVATE stdout' : bumpRefusal || BUMP + '\n',
        stderr: bumpFailure ? 'PRIVATE stderr' : '',
        isStdoutTruncated: false, isStderrTruncated: false } };
    }
    signal();
    await pending;
    const mode = failure;
    failure = 'none';
    if (mode === 'throw') throw new Error('PRIVATE process failure');
    const stdout = mode === 'json' ? 'PRIVATE not JSON' : mode === 'shape'
      ? JSON.stringify({ ...data, waiting: [['PRIVATE malformed row']] }) : JSON.stringify(data);
    return { value: { exitCode: mode === 'exit' ? 1 : 0, stdout, stderr: 'PRIVATE diagnostic',
      isStdoutTruncated: mode === 'truncated', isStderrTruncated: false } };
  });
  return { opened, closed, clock, processes, writes, escaped, opens,
    replace: (value: QueueSnapshot) => { data = value; },
    failNext: (mode: typeof failure) => { failure = mode; },
    failBump: () => { bumpFailure = true; },
    refuseBump: (line: string) => { bumpRefusal = line + '\n'; },
    pause: () => {
      const started = new Promise<void>(resolve => { signal = resolve; });
      pending = new Promise<void>(resolve => { release = resolve; });
      return { started, finish: () => release() };
    },
    clear: () => { processes.length = 0; writes.length = 0; },
  };
}

async function mount($: Engine, surface: 'terminal' | 'desktop' = 'terminal', columns = 170) {
  return $.ui.mount({ plugin: 'unit-board', requestId: 'build-queue', component: 'Pane', surface,
    props: { title: 'Build queue', isFocused: true, bodyColumns: columns,
      scroll: { offset: 0, bodyRows: 100 }, view: {}, placement: 'dock' as const } });
}

describe('build queue pane', () => {
  test('open is intercepted, stores loading before the JSON run and enables Escape', async ($, on) => {
    const fake = queueHost(on);
    await $.session.start(start);
    const pending = fake.pause();
    const opening = $.command.run(command());
    await pending.started;
    expect(fake.writes.map(value => value.kind)).toEqual(['loading']);
    expect(fake.opens).toEqual([{ id: 'build-queue', title: 'Build queue', closeOnEscape: true }]);
    const ui = await mount($);
    expect(drawnText(await ui.drawn())).toContain('Loading build queue…');
    pending.finish();
    expect(await opening).toEqual({ text: 'Build queue opened.' });
    expect(fake.processes).toEqual([[...ARGV, '--json']]);
    expect(fake.writes.map(value => value.kind)).toEqual(['loading', 'queue']);
    expect(fake.escaped).toEqual([]);
  });

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: rows, zone-formatted times, head and totals come from JSON`, async ($, on) => {
      const fake = queueHost(on);
      await $.command.run(command());
      const ui = await mount($, surface, 400);
      const text = drawnText(await ui.drawn()).replace(/\s+/g, ' ');
      expect(text).toContain('widgets title widget-enhancements-title nextest title-tree');
      expect(text).toContain('builds report report check report-tree 14:45 (15 min) 12.0 GiB 30.0 GiB');
      expect(text).toContain(HEAD);
      expect(text.indexOf(HEAD)).toBeLessThan(text.indexOf('Waiting'));
      expect(text).toContain('Totals: promised 10.8 GiB; measured 0.5 GiB; free 30.0 GiB; reserve 3.0 GiB');
      expect(text).toContain('Roster: one session unreadable');
      expect(text).toContain('Roster: another machine unavailable');
      expect(fake.processes).toEqual([[...ARGV, '--json']]);
    });
  }

  test('unchanged ten-second ticks each reread once and replace the displayed queue', async ($, on) => {
    const fake = queueHost(on);
    await $.command.run(command());
    const ui = await mount($);
    fake.clear();
    const changed = snapshot();
    changed.head = { text: 'Head: report check: ready for the next admission check', tone: 'default' };
    changed.waiting[0]![6] = '14:45 (16 min)';
    fake.replace(changed);
    await fake.clock.advance(9_999);
    expect(fake.processes).toEqual([]);
    await fake.clock.advance(1);
    expect(fake.processes).toEqual([[...ARGV, '--json']]);
    const text = drawnText(await ui.drawn()).replace(/\s+/g, ' ');
    expect(text).toContain(changed.head.text);
    expect(text).toContain('14:45 (16 min)');
    expect(text).not.toContain('Stalled:');
    await fake.clock.advance(10_000);
    expect(fake.processes).toEqual([[...ARGV, '--json'], [...ARGV, '--json']]);
  });

  test('a pending read blocks overlapping ticks and repeated opens retain a single timer', async ($, on) => {
    const fake = queueHost(on);
    await $.command.run(command());
    await $.command.run(command());
    fake.clear();
    const pending = fake.pause();
    const ticking = fake.clock.advance(10_000);
    await pending.started;
    await fake.clock.advance(20_000);
    expect(fake.processes).toEqual([[...ARGV, '--json']]);
    pending.finish();
    await ticking;
    await fake.clock.settle();
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.processes).toEqual([[...ARGV, '--json']]);
  });

  test('reload resumes an existing pane with one timer and does no work when closed', async ($, on) => {
    const fake = queueHost(on);
    await $.session.start(start);
    await $.turn.complete(turn);
    await fake.clock.advance(30_000);
    expect(fake.processes).toEqual([]);
    await $.command.run(command());
    await $.session.start(start);
    await $.session.start(start);
    expect(fake.opened).toEqual(['build-queue']);
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.processes).toEqual([[...ARGV, '--json']]);
    expect(await $.command.run(command('close'))).toEqual({ text: 'Build queue closed.' });
    expect(fake.closed).toEqual(['build-queue']);
    fake.clear();
    await $.session.start(start);
    await $.turn.complete(turn);
    await fake.clock.advance(30_000);
    expect(fake.processes).toEqual([]);
    expect(fake.writes).toEqual([]);
    expect(fake.escaped).toEqual([]);
  });

  for (const during of ['open', 'refresh'] as const) {
    test(`closing during a pending ${during} prevents stale writes and further reads`, async ($, on) => {
      const fake = queueHost(on);
      if (during === 'refresh') await $.command.run(command());
      const pending = fake.pause();
      const reading = during === 'open' ? $.command.run(command()) : fake.clock.advance(10_000);
      await pending.started;
      await $.command.run(command('close'));
      fake.clear();
      pending.finish();
      await reading;
      await fake.clock.settle();
      await fake.clock.advance(30_000);
      expect(fake.processes).toEqual([]);
      expect(fake.writes).toEqual([]);
    });
  }

  for (const mode of ['exit', 'json', 'shape', 'truncated', 'throw'] as const) {
    test(`${mode} failure shows a fixed reason and recovers on the next tick`, async ($, on) => {
      const fake = queueHost(on);
      fake.failNext(mode);
      await $.command.run(command());
      const ui = await mount($);
      const text = drawnText(await ui.drawn());
      expect(text).toContain('Build queue could not be read.');
      expect(text).not.toContain('PRIVATE');
      expect(text).not.toContain('widget-enhancements-title');
      await fake.clock.advance(10_000);
      expect(drawnText(await ui.drawn()).replace(/\s+/g, ' ')).toContain(HEAD);
    });
  }

  for (const open of [false, true]) {
    test(`bump passes the selector as one argument and ${open ? 'refreshes the open' : 'does not open a closed'} pane`, async ($, on) => {
      const fake = queueHost(on);
      if (open) await $.command.run(command());
      fake.clear();
      expect(await $.command.run(command('bump worker with spaces'))).toEqual({ text: BUMP });
      expect(fake.processes).toEqual(open
        ? [[...ARGV, 'bump', 'worker with spaces'], [...ARGV, '--json']]
        : [[...ARGV, 'bump', 'worker with spaces']]);
      expect(fake.opened).toEqual(open ? ['build-queue'] : []);
      expect(fake.escaped).toEqual([]);
    });
  }

  test('a bump during a read schedules a fresh snapshot after that read finishes', async ($, on) => {
    const fake = queueHost(on);
    await $.command.run(command());
    fake.clear();
    const pending = fake.pause();
    const ticking = fake.clock.advance(10_000);
    await pending.started;
    expect(await $.command.run(command('bump 2'))).toEqual({ text: BUMP });
    expect(fake.processes).toEqual([[...ARGV, '--json'], [...ARGV, 'bump', '2']]);
    pending.finish();
    await ticking;
    await fake.clock.settle();
    expect(fake.processes).toEqual([[...ARGV, '--json'], [...ARGV, 'bump', '2'], [...ARGV, '--json']]);
  });

  test('a failed bump hides process diagnostics and a missing selector runs nothing', async ($, on) => {
    const fake = queueHost(on);
    fake.failBump();
    expect(await $.command.run(command('bump 9'))).toEqual({ text: 'Build queue could not be read.' });
    expect(fake.processes).toEqual([[...ARGV, 'bump', '9']]);
    fake.clear();
    expect(await $.command.run(command('bump'))).toEqual({ text: 'Build queue could not be read.' });
    expect(fake.processes).toEqual([]);
    expect(fake.escaped).toEqual([]);
  });

  for (const refusal of ["build queue: 'unknown-worker': no waiting step matches",
    "build queue: 'report': ambiguous waiter; matches: 1: report /one, 2: report /two"]) {
    test(`bump reports its refusal: ${refusal}`, async ($, on) => {
      const fake = queueHost(on);
      fake.refuseBump(refusal);
      expect(await $.command.run(command('bump unknown-worker'))).toEqual({ text: refusal });
      expect(fake.processes).toEqual([[...ARGV, 'bump', 'unknown-worker']]);
      expect(fake.opened).toEqual([]);
      expect(fake.escaped).toEqual([]);
    });
  }

  test('an exit-2 bump hides permission errors even when they have the build queue prefix', async ($, on) => {
    mock.env(on, { HOME });
    on('process.run', () => ({ value: { exitCode: 2, stdout: '',
      stderr: "build queue: [Errno 13] Permission denied: '/private/admission/lock'\n",
      isStdoutTruncated: false, isStderrTruncated: false } }));
    expect(await $.command.run(command('bump unknown-worker'))).toEqual({ text: 'Build queue could not be read.' });
  });

  test('an exit-2 locked queue ValueError stays behind the fixed failure reply', async ($, on) => {
    mock.env(on, { HOME });
    on('process.run', () => ({ value: { exitCode: 2, stdout: '',
      stderr: 'build queue: PRIVATE locked queue reading invalid\n',
      isStdoutTruncated: false, isStderrTruncated: false } }));
    expect(await $.command.run(command('bump unknown-worker'))).toEqual({ text: 'Build queue could not be read.' });
  });

  test('an escaped newline in a selector refusal remains a readable single line', async ($, on) => {
    const fake = queueHost(on);
    const refusal = "build queue: 'unknown-worker': unknown waiter; matches: 1: head /tmp/tree\\nsecond";
    fake.refuseBump(refusal);
    expect(await $.command.run(command('bump unknown-worker'))).toEqual({ text: refusal });
  });

  for (const result of ['success', 'refusal'] as const) {
    for (const failedWrites of [1, 2]) {
      test(`a bump ${result} survives ${failedWrites} failed refresh state writes`, async () => {
        type Hook = (services: EngineInterface, event: CommandRunInput) => Promise<unknown>;
        const hooks = new Map<string, Hook>();
        const collect = (event: string, ...args: unknown[]) => {
          const filter = args.length > 1 ? args[0] as { command?: string } : {};
          hooks.set(filter.command ? `${event}:${filter.command}` : event, args.at(-1) as Hook);
          return { catch: () => {} };
        };
        register(collect as On, {});
        let remaining = 0;
        let failures = 0;
        let state: Record<string, QueuePaneState> = {};
        const refusal = "build queue: 'unknown-worker': unknown waiter; matches: none";
        const services = {
          env: { get: async () => HOME },
          ui: { open: async () => ({}) },
          clock: { every: () => ({ cancel: () => {} }) },
          state: {
            get: async () => ({ value: state }),
            set: async (_key: unknown, value: typeof state) => {
              if (remaining > 0) {
                remaining--;
                failures++;
                throw new Error('PRIVATE pane state write failed');
              }
              state = value;
            },
          },
          process: { run: async (argv: string[]) => ({
            exitCode: argv.includes('bump') && result === 'refusal' ? 3 : 0,
            stdout: argv.includes('bump') ? (result === 'refusal' ? refusal : BUMP) : JSON.stringify(snapshot()),
            stderr: '', isStdoutTruncated: false, isStderrTruncated: false,
          }) },
        } as unknown as EngineInterface;
        const run = hooks.get('command.run:build:queue')!;
        await run(services, command());
        remaining = failedWrites;
        expect(await run(services, command('bump unknown-worker'))).toEqual({ text: result === 'refusal' ? refusal : BUMP });
        expect(failures).toBe(failedWrites);
        if (failedWrites === 1) {
          expect(state['build-queue']).toEqual({ kind: 'failed', reason: 'Build queue could not be read.' });
        }
      });
    }
  }

  for (const invalid of ['truncated', 'multiline'] as const) {
    test(`an exit-3 bump hides a ${invalid} refusal`, async ($, on) => {
      mock.env(on, { HOME });
      on('process.run', () => ({ value: { exitCode: 3,
        stdout: invalid === 'truncated' ? "build queue: 'unknown': unknown waiter"
          : "build queue: 'unknown': unknown waiter\nPRIVATE diagnostic\n",
        stderr: '', isStdoutTruncated: invalid === 'truncated', isStderrTruncated: false } }));
      expect(await $.command.run(command('bump unknown'))).toEqual({ text: 'Build queue could not be read.' });
    });
  }
});

describe('queue rendering', () => {
  test('every wide-table text cell aligns left and numeric cell with unit aligns right', async () => {
    const data = snapshot();
    data.running.push(['12', 'b', 'long-unit', 'short', 'build', 'folder-long',
      '2.0 GiB', '12.8 GiB', '-10.8 GiB', 'starting']);
    data.waiting.push(['12', 'long-showrunner', 'r', 'second', 'test', 'long-tree', '1:45 (5 min)',
      '2.0 GiB', '8.0 GiB', 'free memory']);
    const lines = queueLines(data, 800);
    for (const [header, rows] of [[data.running_header, data.running], [data.waiting_header, data.waiting]] as const) {
      const table = lines.filter(line => line.length === header.length
        && line.every((part, column) => part.text.trim() === header[column]));
      expect(table.length).toBe(1);
      const numeric = ['#', 'Promised', 'Using', 'Delta', 'Waiting since', 'Needs', 'Free'];
      const body = rows.map(row => lines.find(line => line.length === header.length
        && line[0]!.text.trim() === row[0] && line[3]!.text.trim() === row[3])!);
      expect(body.every(line => !!line)).toBe(true);
      for (let column = 0; column < header.length; column++) {
        const cells = body.map(line => line[column]!.text);
        if (numeric.includes(header[column]!)) {
          expect(new Set(cells.map(cell => cellWidth(cell.trimEnd()))).size).toBe(1);
          if (['Promised', 'Using', 'Delta', 'Needs', 'Free'].includes(header[column]!)) {
            expect(new Set(cells.map(cell => cell.indexOf('.'))).size).toBe(1);
          }
        } else {
          expect(cells.every(cell => cell === cell.trimStart())).toBe(true);
        }
      }
    }
  });

  test('narrow-table numeric values keep their units together at the right edge', async () => {
    const lines = queueLines(snapshot(), 30);
    for (const heading of ['#', 'Promised', 'Using', 'Delta', 'Waiting since', 'Needs', 'Free']) {
      const numeric = lines.filter(line => line[0]?.text === `${heading}: `);
      expect(numeric.length).toBeGreaterThan(0);
      for (const line of numeric) {
        expect(cellWidth(line.map(part => part.text).join('').trimEnd())).toBe(30);
        expect(line[0]!.text.startsWith(heading)).toBe(true);
        if (['Promised', 'Using', 'Delta', 'Needs', 'Free'].includes(heading)) {
          expect(line[1]!.text.trim()).toMatch(/^-?\d+\.\d GiB$/);
        }
      }
    }
  });

  for (const columns of [400, 60, 30]) {
    test(`${columns} cells keep the complete head explanation, rows and error tone`, async () => {
      const lines = queueLines(snapshot(), columns);
      for (const line of lines) expect(cellWidth(line.map(part => part.text).join(''))).toBeLessThanOrEqual(columns);
      const text = lines.map(line => line.map(part => part.text).join('')).join(' ').replace(/\s+/g, ' ');
      expect(text).toContain(HEAD);
      expect(text).toContain('14:45 (15 min)');
      expect(text).toContain('widget-enhancements-title');
      expect(text).not.toContain('…');
      const error = lines.flat().filter(part => part.tone === 'error').map(part => part.text).join(' ').replace(/\s+/g, ' ');
      expect(error).toBe(HEAD);
      expect(text.indexOf(HEAD)).toBeLessThan(text.indexOf('Waiting'));
    });
  }

  test('empty queues have two empty tables and no head; ordinary heads have no error tone', async () => {
    const data = snapshot();
    data.running = [];
    data.waiting = [];
    data.head = { text: '', tone: 'default' };
    data.problems = [];
    const empty = queueLines(data, 170);
    expect(empty.filter(line => line.map(part => part.text).join('') === 'empty').length).toBe(2);
    expect(empty.flat().some(part => part.text.includes('Stalled:'))).toBe(false);
    data.head = { text: 'Head: worker check: ready for the next admission check', tone: 'default' };
    const ready = queueLines(data, 170).flat();
    expect(ready.find(part => part.text === data.head.text)?.tone).toBe('default');
    expect(ready.some(part => part.tone === 'error')).toBe(false);
  });
});

// The engine kit cannot emit host-origin ui.close. Dispatch the registered hook
// directly for the close mark, Escape and unload lifecycle.
for (const origin of ['person', 'plugin', 'unload'] as const) {
  test(`${origin} close drops queue state, cancels the timer and forwards the host event`, async () => {
    type Hook = (services: EngineInterface, event: unknown, next: (event: unknown) => Promise<unknown>) => unknown;
    const hooks = new Map<string, Hook>();
    const collect = (event: string, ...args: unknown[]) => {
      const filter = args.length > 1 ? args[0] as { command?: string } : {};
      hooks.set(filter.command ? `${event}:${filter.command}` : event, args.at(-1) as Hook);
      return { catch: () => {} };
    };
    register(collect as On, {});
    let state: Record<string, QueuePaneState> = {};
    const ticks: (() => void)[] = [];
    let cancelled = 0;
    let runs = 0;
    let forwarded = 0;
    const services = {
      env: { get: async () => HOME },
      ui: { open: async () => ({}) },
      clock: { every: (_ms: number, tick: () => void) => {
        ticks.push(tick); return { cancel: () => { cancelled++; } };
      } },
      state: { get: async () => ({ value: state }), set: async (_key: unknown, value: typeof state) => { state = value; } },
      process: { run: async () => { runs++; return { exitCode: 0, stdout: JSON.stringify(snapshot()), isStdoutTruncated: false }; } },
    } as unknown as EngineInterface;
    await hooks.get('command.run:build:queue')!(services, command(), async () => { throw new Error('Reached model'); });
    expect(state['build-queue']?.kind).toBe('queue');
    expect(ticks.length).toBe(1);
    await hooks.get('ui.close')!(services, { id: 'build-queue', origin: { kind: origin } }, async value => { forwarded++; return value; });
    expect(state['build-queue']).toBeUndefined();
    expect(cancelled).toBe(1);
    expect(forwarded).toBe(1);
    ticks[0]!();
    await Promise.resolve();
    expect(runs).toBe(1);
  });
}

/** Direct host control makes the ordering of state reads and pane reopens explicit. */
function tablePaneLifetimeHost() {
  type Hook = (services: EngineInterface, event: unknown, next: (event: unknown) => Promise<unknown>) => unknown;
  type StoredTablePane = QueuePaneState | AccountsPaneState<StoredAccountsStatus>;
  const hooks = new Map<string, Hook>();
  const collect = (event: string, ...args: unknown[]) => {
    const filter = args.length > 1 ? args[0] as { command?: string } : {};
    hooks.set(filter.command ? `${event}:${filter.command}` : event, args.at(-1) as Hook);
    return { catch: () => {} };
  };
  register(collect as On, {});
  const states = new Map<string, Record<string, StoredTablePane>>();
  const panes = new Set<string>();
  const runs: string[][] = [];
  const timers: { active: boolean }[] = [];
  let stateReadGate = { pending: Promise.resolve(), started: () => {}, armed: false };
  let accountsGate = { pending: Promise.resolve(), started: () => {} };
  const services = {
    env: { get: async (name: string) => name === 'USER_ZONE' ? 'UTC' : HOME },
    command: { register: async () => ({}) },
    ui: {
      open: async ({ id }: { id: string }) => { panes.add(id); return {}; },
      panes: async () => [...panes].map(id => ({ id })),
    },
    fs: { stat: async () => ({ mtimeMs: NOW - 1 }) },
    clock: { now: async () => NOW, every: () => {
      const timer = { active: true };
      timers.push(timer);
      return { cancel: () => { timer.active = false; } };
    } },
    state: {
      get: async ({ key }: { key: string }) => {
        const value = states.get(key);
        if (stateReadGate.armed) {
          stateReadGate.armed = false;
          stateReadGate.started();
          await stateReadGate.pending;
        }
        return { value };
      },
      set: async ({ key }: { key: string }, value: Record<string, StoredTablePane>) => { states.set(key, value); },
    },
    process: { run: async (argv: string[]) => {
      runs.push(argv);
      const accounts = argv.includes(`${HOME}/.claude/scripts/accounts/accounts.py`);
      if (accounts) {
        accountsGate.started();
        await accountsGate.pending;
      }
      return { exitCode: 0, stdout: JSON.stringify(accounts ? { accounts: [], problems: [] } : snapshot()),
        stderr: '', isStdoutTruncated: false, isStderrTruncated: false };
    } },
  } as unknown as EngineInterface;
  const dispatch = async (event: string, input: unknown) => hooks.get(event)!(services, input, async value => value);
  return { states, panes, runs, timers, dispatch,
    pauseStateRead: () => {
      let finish = () => {};
      const started = new Promise<void>(resolve => { stateReadGate.started = resolve; });
      stateReadGate.pending = new Promise<void>(resolve => { finish = resolve; });
      stateReadGate.armed = true;
      return { started, finish };
    },
    pauseAccountsRead: () => {
      let finish = () => {};
      const started = new Promise<void>(resolve => { accountsGate.started = resolve; });
      accountsGate.pending = new Promise<void>(resolve => { finish = resolve; });
      return { started, finish };
    },
  };
}

for (const pane of [
  { id: 'build-queue', command: 'build:queue', key: 'queuePanes', ready: 'queue' },
  { id: 'accounts-board', command: 'accounts-board', key: 'accountsPanes', ready: 'accounts' },
]) {
  test(`${pane.id}: an older close cannot erase state written by a reopened pane`, async () => {
    const host = tablePaneLifetimeHost();
    const open = () => host.dispatch(`command.run:${pane.command}`, { ...command(), command: pane.command });
    await open();
    const held = host.pauseStateRead();
    const closing = host.dispatch('ui.close', { id: pane.id, origin: { kind: 'person' } });
    await held.started;
    try {
      await open();
      expect(host.states.get(pane.key)?.[pane.id]?.kind).toBe(pane.ready);
    } finally { held.finish(); }
    await closing;
    expect(host.states.get(pane.key)?.[pane.id]?.kind).toBe(pane.ready);
    expect(host.timers.filter(timer => timer.active).length).toBe(1);
  });
}

test('reload reads and schedules the queue while the resumed accounts read is held', async () => {
  const host = tablePaneLifetimeHost();
  host.panes.add('accounts-board');
  host.panes.add('build-queue');
  const held = host.pauseAccountsRead();
  const resuming = host.dispatch('session.start', start);
  await held.started;
  try {
    expect(host.runs).toContainEqual([...ARGV, '--json']);
    expect(host.timers.filter(timer => timer.active).length).toBe(2);
  } finally { held.finish(); }
  await resuming;
  expect(host.states.get('queuePanes')?.['build-queue']?.kind).toBe('queue');
});
