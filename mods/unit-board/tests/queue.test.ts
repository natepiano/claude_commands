import { describe, expect, mock, test } from 'claude-code/testing';
import type { Engine } from 'claude-code/testing';
import type { CommandRunInput, EngineInterface, On, TurnCompleteInput, UiPane } from 'claude-code';
import { register } from '../hooks/register';
import type { QueueLines, QueuePaneState } from '../types/queue';
import type { AccountsPaneState } from '../types';
import type { StoredAccountsStatus } from '../types/accounts';
import { CWD, drawnText, inspectDrawing, NOW, SESSION, sessionFixture } from './kit';

const HOME = '/test-home';
const ARGV = [`${HOME}/.claude/scripts/lib/py`, `${HOME}/.claude/scripts/build_queue/build_queue.py`];
const READ = [...ARGV, '--json', '--columns', '100', '--you', SESSION];
const command = (args = ''): CommandRunInput => ({ command: 'build:queue', args,
  origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 170 } });
const start = { cwd: CWD, surface: null, isInteractive: false } as const;
const turn: TurnCompleteInput = { answer: 'finished', reason: 'answer', durationMs: 100,
  isAborted: false, turnId: 'queue-turn' };
const HEAD = 'Build queue 10:46:12 · 1 running · 1 waiting · #1 waits on memory pressure, budget';
const BUMP = 'tester moved report: check in /tmp/report to the head; memory limits still apply';

function snapshot(): QueueLines {
  return {
    columns: 100, as_of: 1793360772,
    lines: [
      [{ text: HEAD, bold: true }],
      [],
      [{ text: 'Running', bold: true }],
      [{ text: '●', tone: 'success' },
        { text: '    ↑29   0 min      5.1  nextest   11.1  tool-based-ui · widget · ' },
        { text: 'widget-enhancements-title', tone: 'inactive' }],
      [{ text: 'Waiting', bold: true }],
      [{ text: '■', tone: 'error' },
        { text: '  1 ↑27   6 min   ~1 min  nextest   11.5  tool-based-ui · cables · ' },
        { text: 'work-lead', tone: 'inactive' }],
      [{ text: '         #1 waits on  ' },
        { text: '✗ memory pressure: full avg10 7.7 now; starts below 5.0', tone: 'error' }],
      [{ text: '                     ✓ free memory 24.5 GiB; needs 20.5', tone: 'success' }],
      [{ text: 'Memory in GiB. starts: an estimate at the last hour\'s pace, 44 steps an hour.', tone: 'warning' }],
    ],
  };
}

/** The kit returns a semantic tree and never paints the host's viewport. */
function expectHostClipping(tree: unknown, columns: number, lineCount: number) {
  expect((tree as { props: Record<string, unknown> }).props).toMatchObject({ width: columns, overflow: 'hidden' });
  const drawing = inspectDrawing(tree);
  expect(drawing.rows.length).toBe(lineCount);
  for (const props of drawing.rows) {
    expect(props).toMatchObject({ height: 1, overflow: 'hidden' });
  }
  for (const props of drawing.textProps) expect(props).toMatchObject({ wrap: 'truncate' });
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
  let invalid: unknown;
  let bumpFailure = false;
  let bumpRefusal = '';
  let pending = Promise.resolve();
  let signal = () => {};
  let release = () => {};
  const restorePane = (id: string, title = id) => {
    panes.set(id, { id, title, isShown: true, isFocused: true, isPlaced: true });
  };
  on('session.id', async () => ({ value: await sessionFixture.id() }));
  on('session.start', (_$, e) => ({ cwd: e.cwd }));
  on('turn.complete', (_$, e) => ({ text: e.answer }));
  on('command.register', (_$, e) => ({ value: { command: e.name } }));
  on('command.run', (_$, e) => { escaped.push(e.command); return { text: 'Reached the model.' }; });
  on('ui.panes', () => ({ value: [...panes.values()] }));
  on('ui.open', (_$, e) => {
    opens.push(e);
    opened.push(e.id);
    restorePane(e.id, e.title);
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
    const columns = Number(e.argv[e.argv.indexOf('--columns') + 1]);
    const stdout = mode === 'json' ? 'PRIVATE not JSON' : mode === 'shape'
      ? JSON.stringify(invalid === undefined ? { ...data, lines: [[{ text: 'PRIVATE malformed span', tone: 'PRIVATE' }]] } : invalid)
      : JSON.stringify({ ...data, columns });
    return { value: { exitCode: mode === 'exit' ? 1 : 0, stdout, stderr: 'PRIVATE diagnostic',
      isStdoutTruncated: mode === 'truncated', isStderrTruncated: false } };
  });
  return { opened, closed, clock, processes, writes, escaped, opens,
    restore: () => restorePane('build-queue', 'Build queue'),
    replace: (value: QueueLines) => { data = value; },
    invalidNext: (value: unknown) => { invalid = value; failure = 'shape'; },
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

async function mount($: Engine, surface: 'terminal' | 'desktop' = 'terminal', columns = 100) {
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
    expect(fake.processes).toEqual([READ]);
    expect(fake.writes.map(value => value.kind)).toEqual(['loading', 'queue']);
    expect(fake.escaped).toEqual([]);
  });

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: paints every supplied span in order with its tone and bold style`, async ($, on) => {
      const fake = queueHost(on);
      await $.command.run(command());
      const ui = await mount($, surface);
      const tree = await ui.drawn();
      expect(inspectDrawing(tree).lines).toEqual(snapshot().lines.map(line => line.map(part => part.text).join('')));
      expect(inspectDrawing(tree).spans).toEqual(snapshot().lines.flat().map(part => ({
        text: part.text, color: part.tone ?? 'default', bold: part.bold ?? false,
      })));
      expect(inspectDrawing(tree).textProps.length).toBe(snapshot().lines.flat().length);
      expect(fake.processes).toEqual([READ]);
    });
  }

  test('a span with omitted style fields paints with the default foreground, background and weight', async ($, on) => {
    const fake = queueHost(on);
    const data = snapshot();
    data.lines = [[{ text: 'unstyled queue text' }]];
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    const drawing = inspectDrawing(await ui.drawn());
    expect(drawing.lines).toEqual(['unstyled queue text']);
    expect(drawing.spans).toEqual([{ text: 'unstyled queue text', color: 'default', bold: false }]);
    expect(drawing.textProps).toHaveLength(1);
    const props = drawing.textProps[0];
    if (!props) throw new Error('The default span must paint one Text element.');
    expect(props.color ?? 'default').toBe('default');
    expect(props.backgroundColor ?? 'default').toBe('default');
    expect(props.bold).toBe(false);
    expect(fake.processes).toEqual([READ]);
  });

  test('a countdown repaints every second with its background and no extra script run', async ($, on) => {
    const fake = queueHost(on);
    const data = snapshot();
    data.lines = [[{ text: '▍', tone: 'success', bold: false, background: 'suggestion' },
      { text: ' ⊘ ', tone: 'error', bold: false },
      { text: '1:10', tone: 'error', bold: false, until: NOW / 1000 + 70 }]];
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    expect(inspectDrawing(await ui.drawn()).lines.join('\n')).toBe('▍ ⊘ 1:10');
    const boundary = inspectDrawing(await ui.drawn()).textProps.find(props => props.backgroundColor === 'suggestion');
    expect(boundary).toMatchObject({ color: 'success', backgroundColor: 'suggestion' });
    await fake.clock.advance(1000);
    expect(inspectDrawing(await ui.drawn()).lines.join('\n')).toBe('▍ ⊘ 1:09');
    await fake.clock.advance(8000);
    expect(inspectDrawing(await ui.drawn()).lines.join('\n')).toBe('▍ ⊘ 1:01');
    expect(fake.processes).toEqual([READ]);
    await fake.clock.advance(1000);
    expect(fake.processes).toEqual([READ, READ]);
    await $.command.run(command('close'));
    fake.clear();
    await fake.clock.advance(5000);
    expect(fake.processes).toEqual([]);
    expect(fake.writes).toEqual([]);
  });

  for (const suspendedRead of ['clock', 'panes'] as const) {
    test(`closing while a render reads ${suspendedRead} cannot restart its countdown timer`, async ($, on) => {
      const timerRequests: number[] = [];
      let signal = () => {};
      let finish = () => {};
      let holdRead = false;
      const started = new Promise<void>(resolve => { signal = resolve; });
      const pending = new Promise<void>(resolve => { finish = resolve; });
      // Outer observers leave the shared host's exact event hooks in charge.
      on('clock.*', async (_$, event, next) => {
        if (next.event === 'clock.every' && 'ms' in event) timerRequests.push(event.ms);
        if (next.event === 'clock.now' && suspendedRead === 'clock' && holdRead) {
          holdRead = false;
          signal();
          await pending;
        }
        return next(event);
      });
      on('ui.*', async (_$, event, next) => {
        const result = await next(event);
        if (next.event === 'ui.panes' && suspendedRead === 'panes' && holdRead) {
          holdRead = false;
          // Save the open-pane snapshot before close, then return it after close.
          signal();
          await pending;
        }
        return result;
      });
      const fake = queueHost(on);
      const data = snapshot();
      data.lines = [[{ text: '1:10', tone: 'error', until: NOW / 1000 + 70 }]];
      fake.replace(data);
      await $.command.run(command());
      holdRead = true;
      const painting = mount($).then(ui => ui.drawn());
      await started;
      try {
        await $.command.run(command('close'));
      } finally { finish(); }
      await painting;
      expect(timerRequests).toEqual([10_000]);
      fake.clear();
      await fake.clock.advance(20_000);
      expect(fake.processes).toEqual([]);
      expect(fake.writes).toEqual([]);
    });
  }

  test('an expired countdown clamps to zero until the next script refresh', async ($, on) => {
    const fake = queueHost(on);
    const data = snapshot();
    data.lines = [[{ text: '0:01', tone: 'success', bold: false, until: NOW / 1000 + 1 }]];
    fake.replace(data);
    await $.command.run(command());
    const ui = await mount($);
    await fake.clock.advance(2000);
    expect(drawnText(await ui.drawn())).toBe('0:00');
    expect(fake.processes).toEqual([READ]);
  });

  test('a selector opens a card, survives timer refresh and reload, and a bare command restores the view', async ($, on) => {
    const fake = queueHost(on);
    const data = snapshot();
    data.lines = [[{ text: '#4 of 32 · cargo-liner · berth · clippy · 2.9 GiB', tone: 'default', bold: true }],
      [{ text: 'Next? No. 3 steps are ahead of it: 2 moved up, 1 joined earlier.', tone: 'default', bold: false }]];
    fake.replace(data);
    expect(await $.command.run(command('4'))).toEqual({ text: 'Build queue opened.' });
    const ui = await mount($);
    expect(drawnText(await ui.drawn())).toContain('Next? No. 3 steps are ahead');
    expect(fake.processes).toEqual([[...READ, '4']]);
    expect(fake.writes.at(-1)).toEqual({ kind: 'queue', data, selection: { kind: 'card', selector: '4' } });
    await fake.clock.advance(10_000);
    await $.session.start(start);
    expect(fake.processes).toEqual([[...READ, '4'], [...READ, '4'], [...READ, '4']]);
    await $.command.run(command());
    expect(fake.processes.at(-1)).toEqual(READ);
    expect(fake.writes.at(-1)).toEqual({ kind: 'queue', data, selection: { kind: 'view' } });
  });

  test('a session or folder selector stays one process argument', async ($, on) => {
    const fake = queueHost(on);
    await $.command.run(command('/tmp/work tree'));
    expect(fake.processes).toEqual([[...READ, '/tmp/work tree']]);
    expect(fake.escaped).toEqual([]);
  });

  test('a render during loading records its width for the next read', async ($, on) => {
    const fake = queueHost(on);
    const held = fake.pause();
    const opening = $.command.run(command());
    await held.started;
    try {
      const ui = await mount($, 'terminal', 89);
      expect(drawnText(await ui.drawn())).toContain('Loading build queue…');
    } finally { held.finish(); }
    await opening;
    await fake.clock.settle();
    await fake.clock.advance(10_000);
    expect(fake.processes[0]).toEqual(READ);
    expect(fake.processes.at(-1)).toEqual([...ARGV, '--json', '--columns', '89', '--you', SESSION]);
  });

  test('repeated renders at a new width join one forced refresh while a read is busy', async ($, on) => {
    const fake = queueHost(on);
    await $.command.run(command('4'));
    fake.clear();
    const held = fake.pause();
    const ticking = fake.clock.advance(10_000);
    await held.started;
    const ui = await mount($, 'terminal', 89);
    try {
      await ui.redraw();
      await ui.redraw();
      expect(fake.processes).toEqual([[...READ, '4']]);
    } finally { held.finish(); }
    await ticking;
    await fake.clock.settle();
    expect(fake.processes).toEqual([[...READ, '4'], [...ARGV, '--json', '--columns', '89', '--you', SESSION, '4']]);
    await ui.redraw();
    await ui.redraw();
    await fake.clock.settle();
    expect(fake.processes.length).toBe(2);
    await fake.clock.advance(10_000);
    expect(fake.processes.at(-1)).toEqual([...ARGV, '--json', '--columns', '89', '--you', SESSION, '4']);
  });

  test('a width change outside a timer tick immediately requests a fresh render', async ($, on) => {
    const fake = queueHost(on);
    await $.command.run(command());
    fake.clear();
    const ui = await mount($, 'terminal', 80);
    await ui.drawn();
    await fake.clock.settle();
    expect(fake.processes).toEqual([[...ARGV, '--json', '--columns', '80', '--you', SESSION]]);
    await ui.drawn();
    await fake.clock.settle();
    expect(fake.processes.length).toBe(1);
  });

  test('unchanged ten-second ticks each reread once and replace the displayed queue', async ($, on) => {
    const fake = queueHost(on);
    await $.command.run(command());
    const ui = await mount($);
    fake.clear();
    const changed = snapshot();
    changed.lines[0] = [{ text: 'Build queue 10:46:22 · #1 starts at the next check', tone: 'default', bold: true }];
    changed.lines[5]![1]!.text = '  1 ↑27   7 min   ~1 min  nextest   11.5  ';
    fake.replace(changed);
    await fake.clock.advance(9_999);
    expect(fake.processes).toEqual([]);
    await fake.clock.advance(1);
    expect(fake.processes).toEqual([READ]);
    const text = drawnText(await ui.drawn()).replace(/\s+/g, ' ');
    expect(text).toContain(changed.lines[0]![0]!.text);
    expect(text).toContain('7 min');
    expect(text).not.toContain(HEAD);
    await fake.clock.advance(10_000);
    expect(fake.processes).toEqual([READ, READ]);
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
    expect(fake.processes).toEqual([READ]);
    pending.finish();
    await ticking;
    await fake.clock.settle();
    fake.clear();
    await fake.clock.advance(10_000);
    expect(fake.processes).toEqual([READ]);
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
    expect(fake.processes).toEqual([READ]);
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

  const invalidShapes: [string, unknown][] = [
    ['null document', null],
    ['legacy row tables', { running: [], waiting: [], head: { text: HEAD, tone: 'error' } }],
    ['missing columns', { as_of: 1, lines: [] }],
    ['string columns', { columns: '100', as_of: 1, lines: [] }],
    ['zero columns', { columns: 0, as_of: 1, lines: [] }],
    ['fractional columns', { columns: 89.5, as_of: 1, lines: [] }],
    ['missing as_of', { columns: 100, lines: [] }],
    ['string as_of', { columns: 100, as_of: 'PRIVATE time', lines: [] }],
    ['null as_of', { columns: 100, as_of: null, lines: [] }],
    ['missing lines', { columns: 100, as_of: 1 }],
    ['null lines', { columns: 100, as_of: 1, lines: null }],
    ['non-array line', { columns: 100, as_of: 1, lines: ['PRIVATE line'] }],
    ['null span', { columns: 100, as_of: 1, lines: [[null]] }],
    ['numeric text', { columns: 100, as_of: 1, lines: [[{ text: 4, tone: 'default', bold: false }]] }],
    ['null tone', { columns: 100, as_of: 1, lines: [[{ text: 'PRIVATE text', tone: null }]] }],
    ['unknown tone', { columns: 100, as_of: 1, lines: [[{ text: 'PRIVATE text', tone: 'PRIVATE', bold: false }]] }],
    ['unknown background', { columns: 100, as_of: 1, lines: [[{ text: 'x', tone: 'default', bold: false, background: 'PRIVATE' }]] }],
    ['string deadline', { columns: 100, as_of: 1, lines: [[{ text: 'x', tone: 'default', bold: false, until: 'PRIVATE' }]] }],
    ['null deadline', { columns: 100, as_of: 1, lines: [[{ text: 'x', tone: 'default', bold: false, until: null }]] }],
    ['null bold', { columns: 100, as_of: 1, lines: [[{ text: 'PRIVATE text', bold: null }]] }],
    ['string bold', { columns: 100, as_of: 1, lines: [[{ text: 'PRIVATE text', tone: 'default', bold: 'yes' }]] }],
  ];
  for (const [name, invalid] of invalidShapes) {
    test(`invalid QueueLines ${name} shows only the fixed failure and then recovers`, async ($, on) => {
      const fake = queueHost(on);
      fake.invalidNext(invalid);
      await $.command.run(command());
      const ui = await mount($);
      expect(drawnText(await ui.drawn())).toBe('Build queue could not be read.');
      await fake.clock.advance(10_000);
      expect(drawnText(await ui.drawn())).toContain(HEAD);
    });
  }

  for (const open of [false, true]) {
    test(`bump passes the selector as one argument and ${open ? 'refreshes the open' : 'does not open a closed'} pane`, async ($, on) => {
      const fake = queueHost(on);
      if (open) await $.command.run(command());
      fake.clear();
      expect(await $.command.run(command('bump worker with spaces'))).toEqual({ text: BUMP });
      expect(fake.processes).toEqual(open
        ? [[...ARGV, 'bump', 'worker with spaces'], READ]
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
    expect(fake.processes).toEqual([READ, [...ARGV, 'bump', '2']]);
    pending.finish();
    await ticking;
    await fake.clock.settle();
    expect(fake.processes).toEqual([READ, [...ARGV, 'bump', '2'], READ]);
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
          session: sessionFixture,
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
          expect(state['build-queue']).toEqual({ kind: 'failed', reason: 'Build queue could not be read.', selection: { kind: 'view' } });
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
  for (const surface of ['terminal', 'desktop'] as const) {
    for (const columns of [40, 60, 80, 100, 140]) {
      test(`${surface}: ${columns} columns clips each supplied step once without record fallback`, async ($, on) => {
        const fake = queueHost(on);
        const rows = [
          '  1 ↑27   6 min   ~1 min  nextest   11.5  tool-based-ui / cables / work-lead-with-a-very-long-session-name-that-crosses-the-right-edge-of-the-pane',
          '  2      39 min   ~4 min  mend       7.6  tool-based-ui / hana-migration',
        ];
        fake.replace({ columns: 100, as_of: 1, lines: [[], ...rows.map(text => [
          { text, tone: 'default' as const, bold: false },
        ]), [], [{ text: 'Memory in GiB.', tone: 'warning', bold: false }]] });
        await $.command.run(command());
        const ui = await mount($, surface, columns);
        const tree = await ui.drawn();
        expect(inspectDrawing(tree).lines).toEqual(['', ...rows, '', 'Memory in GiB.']);
        expectHostClipping(tree, columns, 5);
      });
    }
  }

  test('clipping preserves a wide cell and combining mark at the edge, with the original span style', async ($, on) => {
    const fake = queueHost(on);
    fake.replace({ columns: 100, as_of: 1, lines: [[
      { text: '■ 1 ', tone: 'error', bold: true },
      { text: '界e\u0301 overflowing', tone: 'inactive', bold: false },
    ]] });
    await $.command.run(command());
    const ui = await mount($, 'terminal', 7);
    const tree = await ui.drawn();
    expect(inspectDrawing(tree).lines).toEqual(['■ 1 界e\u0301 overflowing']);
    expectHostClipping(tree, 7, 1);
    expect(inspectDrawing(tree).spans).toEqual([
      { text: '■ 1 ', color: 'error', bold: true },
      { text: '界e\u0301 overflowing', color: 'inactive', bold: false },
    ]);
  });

  test('valid empty lines and an empty queue are painted without invented table headings or records', async ($, on) => {
    const fake = queueHost(on);
    fake.replace({ columns: 100, as_of: 1, lines: [[], []] });
    await $.command.run(command());
    const ui = await mount($);
    expect(inspectDrawing(await ui.drawn()).lines).toEqual(['', '']);
    fake.replace({ columns: 100, as_of: 2, lines: [] });
    await fake.clock.advance(10_000);
    expect(drawnText(await ui.drawn())).toBe('');
    expect(inspectDrawing(await ui.drawn()).textProps).toEqual([]);
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
      session: sessionFixture,
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
  const timers: { active: boolean; tick: () => void }[] = [];
  let failQueue = false;
  let queueWritten = () => {};
  let stateReadGate = { pending: Promise.resolve(), started: () => {}, armed: false };
  let accountsGate = { pending: Promise.resolve(), started: () => {} };
  const services = {
    session: sessionFixture,
    env: { get: async (name: string) => name === 'USER_ZONE' ? 'UTC' : HOME },
    command: { register: async () => ({}) },
    ui: {
      open: async ({ id }: { id: string }) => { panes.add(id); return {}; },
      panes: async () => [...panes].map(id => ({ id })),
    },
    fs: { stat: async () => ({ mtimeMs: NOW - 1 }) },
    clock: { now: async () => NOW, every: (_ms: number, tick: () => void) => {
      const timer = { active: true, tick };
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
      set: async ({ key }: { key: string }, value: Record<string, StoredTablePane>) => {
        states.set(key, value);
        if (key === 'queuePanes' && ['queue', 'failed'].includes(value['build-queue']?.kind ?? '')) queueWritten();
      },
    },
    process: { run: async (argv: string[]) => {
      runs.push(argv);
      const accounts = argv.includes(`${HOME}/.claude/scripts/accounts/accounts.py`);
      if (accounts) {
        accountsGate.started();
        await accountsGate.pending;
      }
      const exitCode = !accounts && failQueue ? 1 : 0;
      if (!accounts) failQueue = false;
      return { exitCode, stdout: JSON.stringify(accounts ? { accounts: [], problems: [] } : snapshot()),
        stderr: '', isStdoutTruncated: false, isStderrTruncated: false };
    } },
  } as unknown as EngineInterface;
  const dispatch = async (event: string, input: unknown) => hooks.get(event)!(services, input, async value => value);
  return { states, panes, runs, timers, dispatch,
    failNextQueue: () => { failQueue = true; },
    tick: async () => {
      const written = new Promise<void>(resolve => { queueWritten = resolve; });
      for (const timer of timers) if (timer.active) timer.tick();
      await written;
    },
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
    expect(host.runs).toContainEqual(READ);
    expect(host.timers.filter(timer => timer.active).length).toBe(2);
  } finally { held.finish(); }
  await resuming;
  expect(host.states.get('queuePanes')?.['build-queue']?.kind).toBe('queue');
});

test('a saved table pane without a selector resumes the whole queue', async () => {
  const host = tablePaneLifetimeHost();
  // Persisted input from the previous producer deliberately predates QueueLines.
  const legacy = { kind: 'queue', data: {
    running_header: [], running: [], waiting_header: [], waiting: [],
    head: { text: 'old head', tone: 'default' },
    totals: { promised: '0', measured: '0', free: '24', reserve: '3' }, problems: [],
  } };
  host.states.set('queuePanes', { 'build-queue': legacy as unknown as QueuePaneState });
  host.panes.add('build-queue');
  await host.dispatch('session.start', start);
  expect(host.runs).toEqual([READ]);
  expect(host.states.get('queuePanes')?.['build-queue']).toEqual({ kind: 'queue', data: snapshot(), selection: { kind: 'view' } });
});

for (const savedSelector of ['4', null] as const) {
  test(`legacy saved selector ${savedSelector} converts once and survives refresh and reload`, async () => {
    const host = tablePaneLifetimeHost();
    const legacy = { kind: 'queue', data: snapshot(), selector: savedSelector };
    host.states.set('queuePanes', { 'build-queue': legacy as unknown as QueuePaneState });
    host.panes.add('build-queue');
    await host.dispatch('session.start', start);
    const selection = savedSelector === null ? { kind: 'view' } : { kind: 'card', selector: savedSelector };
    expect(host.states.get('queuePanes')?.['build-queue']).toEqual({ kind: 'queue', data: snapshot(), selection });
    await host.tick();
    await host.dispatch('session.start', start);
    const argv = savedSelector === null ? READ : [...READ, savedSelector];
    expect(host.runs).toEqual([argv, argv, argv]);
    expect(host.states.get('queuePanes')?.['build-queue']).toEqual({ kind: 'queue', data: snapshot(), selection });
  });
}

test('a failed card read retains its selector through a fresh module reload', async ($, on) => {
  const host = tablePaneLifetimeHost();
  host.failNextQueue();
  await host.dispatch('command.run:build:queue', command('4'));
  expect(host.states.get('queuePanes')?.['build-queue']?.kind).toBe('failed');
  expect(host.runs).toEqual([[...READ, '4']]);
  // This Engine has not loaded the plugin yet. Its first call instantiates a
  // fresh hooks module, distinct from the test's directly imported register.
  const fake = queueHost(on);
  let persisted = host.states.get('queuePanes') as Record<string, QueuePaneState>;
  on('state.get', { plugin: 'unit-board', key: 'queuePanes' }, () => ({ value: { value: persisted, version: 1 } }));
  on('state.set', { plugin: 'unit-board', key: 'queuePanes' }, (_$, event, next) => {
    persisted = event.value;
    return next(event);
  });
  fake.restore();
  await $.session.start(start);
  expect(fake.processes).toEqual([[...READ, '4']]);
  const ui = await mount($);
  expect(drawnText(await ui.drawn())).toContain(HEAD);
  expect(drawnText(await ui.drawn())).not.toContain('Build queue could not be read.');
});

test('an older reload cannot replace a card selected while its saved-state read is held', async () => {
  const host = tablePaneLifetimeHost();
  host.states.set('queuePanes', { 'build-queue': { kind: 'queue', data: snapshot(), selection: { kind: 'view' } } });
  host.panes.add('build-queue');
  const held = host.pauseStateRead();
  const reloading = host.dispatch('session.start', start);
  await held.started;
  try {
    await host.dispatch('command.run:build:queue', command('4'));
  } finally { held.finish(); }
  await reloading;
  await host.tick();
  expect(host.runs).toEqual([[...READ, '4'], [...READ, '4']]);
  expect(host.states.get('queuePanes')?.['build-queue']).toEqual({ kind: 'queue', data: snapshot(), selection: { kind: 'card', selector: '4' } });
});
