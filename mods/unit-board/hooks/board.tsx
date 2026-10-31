import type { Elements } from 'claude-code';

export type PaneElements = Pick<Elements['terminal'], 'Box' | 'Text' | 'Button'>;
import type { BoardData, BoardUnit } from '../types/board';
import type { BoardSnapshot } from '../types';
import type { Tone } from './geometry';
import { cellWidth, clockText, doneText, elapsedText, overdueText, rulerCells, runsOf, shortTime, signedMinutes, stripCells, wrappedLines } from './geometry';

/** The detail view retains the complete ETA history. */
export function etaText(unit: BoardUnit, now = 0): string {
  const eta = unit.eta;
  if (!eta?.at) return '';
  const history = eta.moved > 0
    ? `first ${eta.first?.text ?? '—'} · moved ${eta.moved}× · ${signedMinutes(eta.delta_minutes ?? 0)} ${(eta.delta_minutes ?? 0) < 0 ? 'early' : 'late'}`
    : 'on its first ETA';
  const overdue = unit.phase ? overdueText(eta.at.at, now) : '';
  return `ETA ${eta.at.text}${overdue}  ${history} · fix rounds ${eta.fix_done}${eta.fix_running ? ' + 1 running' : ''}`;
}

export function gateText(unit: BoardUnit): string {
  const phase = unit.phases.find(row => row.status === 'gated');
  if (!phase || phase.gate.kind === 'none') return '';
  const gate = phase.gate;
  return gate.kind === 'timed'
    ? `Phase ${phase.id} gated until ${gate.until.text} — ${gate.gate}: ${gate.waits_on}`
    : `Phase ${phase.id} gated by ${gate.gate}: ${gate.waits_on}`;
}

export function finishText(unit: BoardUnit, now = 0): string {
  const finish = unit.finish;
  switch (finish.kind) {
    case 'predicted': return `done by ${finish.at.text}${overdueText(finish.at.at, now)}`;
    case 'finished': return `finished ${finish.at.text}`;
    case 'after_gate': return `after ${finish.gate}`;
    case 'unknown': return '';
  }
}

function boardGateText(unit: BoardUnit, now: number, utcOffsetMinutes: number): string {
  const phase = unit.phases.find(row => row.status === 'gated');
  if (!phase || phase.gate.kind === 'none') return '';
  const gate = phase.gate;
  return `Phase ${phase.id} waits on ${gate.gate}${gate.kind === 'timed' ? ` until ${shortTime(gate.until, now, utcOffsetMinutes)}` : ''}: ${gate.waits_on}`;
}

function runFinishText(unit: BoardUnit, now: number, utcOffsetMinutes: number): string {
  const finish = unit.finish;
  switch (finish.kind) {
    case 'predicted': return `run ends ${shortTime(finish.at, now, utcOffsetMinutes)}${finish.at.at < now ? ` · late ${elapsedText(now - finish.at.at)}` : ''}`;
    case 'finished': return `run ended ${shortTime(finish.at, now, utcOffsetMinutes)}`;
    case 'after_gate': return `run ends after ${finish.gate}`;
    case 'unknown': return '';
  }
}

/** The board has no ruler; the detail view uses this shared renderer. */
export function RulerView(elements: PaneElements, unit: BoardUnit, data: BoardData, now: number, width: number) {
  const { Box, Text } = elements;
  if (!unit.phase) return <Box />;
  const ruler = rulerCells(unit, now, width, data.utc_offset_minutes);
  const paint = (cells: typeof ruler.track) => runsOf(cells).map(run => run.tone === 'default'
    ? <Text>{run.text}</Text> : <Text color={run.tone}>{run.text}</Text>);
  return <Box flexDirection="column">
    <Text>{paint(ruler.track)}</Text>
    <Text>{paint(ruler.labelCells)}</Text>
  </Box>;
}

function workerCounts(unit: BoardUnit): [string, number][] {
  const counts = new Map<string, number>();
  for (const worker of unit.workers) {
    if (worker.state !== 'ended') counts.set(worker.state, (counts.get(worker.state) ?? 0) + 1);
  }
  const priority = (state: string) => ['running', 'idle', 'paused'].indexOf(state);
  return [...counts].sort(([left], [right]) => {
    const a = priority(left), b = priority(right);
    return (a < 0 ? 3 : a) - (b < 0 ? 3 : b) || left.localeCompare(right, undefined, { sensitivity: 'base' });
  });
}

export function workersText(unit: BoardUnit): string {
  return workerCounts(unit).map(([state, count]) => `${count} ${state}`).join(' · ');
}

export function healthTone(unit: BoardUnit, now: number): Tone {
  if ((unit.eta?.at && unit.eta.at.at < now) || (unit.finish.kind === 'predicted' && unit.finish.at.at < now)) return 'error';
  if (unit.phases.some(phase => phase.status === 'gated') || (unit.phase && !unit.eta?.at)) return 'warning';
  if (unit.state === 'running') return 'suggestion';
  if (unit.state === 'run-finished') return 'success';
  return 'inactive';
}

/** Styling stays attached to text even when a fact wraps onto another row. */
export interface BoardSpan { text: string; tone: Tone; kind: 'fact' | 'name' | 'bold' }
export type BoardLine = BoardSpan[];
interface UnitBlock { unit: BoardUnit; lines: BoardLine[]; workers: BoardLine[] }
export const span = (text: string, tone: Tone = 'default', kind: BoardSpan['kind'] = 'fact'): BoardSpan => ({ text, tone, kind });
const lineText = (parts: BoardLine) => parts.map(part => part.text).join('');

export function wrapLine(parts: BoardLine, width: number, indent = 0): BoardLine[] {
  indent = Math.max(0, Math.min(width - 1, indent));
  const text = lineText(parts);
  const room = Math.max(1, width - indent);
  const wrapped = wrappedLines(text, room, indent ? 0 : 2);
  let cursor = 0;
  return wrapped.map(line => {
    const content = line.trimStart();
    const start = text.indexOf(content, cursor);
    const end = start + content.length;
    const row: BoardLine = [span(' '.repeat(indent) + line.slice(0, line.length - content.length))];
    let offset = 0;
    for (const part of parts) {
      const from = Math.max(start, offset);
      const to = Math.min(end, offset + part.text.length);
      if (from < to) row.push({ ...part, text: part.text.slice(from - offset, to - offset) });
      offset += part.text.length;
    }
    cursor = end;
    return row;
  });
}

/** A right-hand fact moves intact to its next row before either side wraps. */
export function pairedLines(left: BoardLine, right: BoardLine, width: number, indent = 2, rightIndent = indent): BoardLine[] {
  if (!lineText(left)) return wrapLine(right, width, rightIndent);
  if (!lineText(right)) return wrapLine(left, width, indent);
  const spacing = width - indent - cellWidth(lineText(left)) - cellWidth(lineText(right));
  if (spacing >= 2) {
    return wrapLine([...left, span(' '.repeat(spacing)), ...right], width, indent);
  }
  return [...wrapLine(left, width, indent), ...wrapLine(right, width, rightIndent)];
}

function unitBlock(unit: BoardUnit, data: BoardData, now: number, width: number): UnitBlock {
  const done = unit.phases.filter(phase => phase.status === 'done').length;
  const progress = `${done}/${unit.phases.length}`;
  const finish = runFinishText(unit, now, data.utc_offset_minutes);
  const finishTone = unit.finish.kind === 'predicted' && unit.finish.at.at < now ? 'error' : 'default';
  const heading = [span('● ', healthTone(unit, now)), span(unit.key, 'default', 'name')];
  if (!unit.phase) {
    heading.push(span(`  ${unit.stage}${unit.phases.length ? ` · ${progress}` : ''}`));
    if (finish) heading.push(span(` · ${finish}`, finishTone));
    return { unit, lines: wrapLine(heading, width), workers: [] };
  }
  const phase = unit.phase;
  heading.push(span(phase.kind === 'ticket' ? `  Ticket ${phase.id} · ${phase.merged} merged  ${phase.title}` : `  Phase ${phase.id}  ${phase.title}`));
  const left: BoardLine = runsOf(stripCells(unit.phases)).map(run => span(run.text, run.tone));
  if (left.length) left.push(span('  '));
  left.push(span(progress));
  if (unit.stage !== `Phase ${phase.id} running` && unit.stage !== `Ticket ${phase.id} running` && unit.stage) left.push(span(`  ${unit.stage}`));
  const fixes = unit.eta?.fix_done ?? unit.phases.find(row => row.id === phase.id)?.fix_done ?? 0;
  const fixing = unit.eta?.fix_running ?? unit.phases.find(row => row.id === phase.id)?.fix_running ?? 0;
  if (fixes > 0) left.push(span(` · fix rounds ${fixes}${fixing ? ' + 1 running' : ''}`, fixes >= 3 ? 'warning' : 'default'));
  const eta = unit.eta?.at;
  const right = eta ? [span(`ETA ${shortTime(eta, now, data.utc_offset_minutes)} · ${eta.at < now ? 'late' : 'in'} ${elapsedText(eta.at - now)}`, eta.at < now ? 'error' : 'default')]
    : [span('no ETA', 'warning')];
  const lines = [...wrapLine(heading, width), ...pairedLines(left, right, width)];
  const gate = boardGateText(unit, now, data.utc_offset_minutes);
  if (gate) lines.push(...wrapLine([span(gate, 'warning')], width, 2));
  const workers: BoardLine = [];
  const counts = workerCounts(unit);
  if (counts.length) {
    workers.push(span('workers ', 'inactive'));
    counts.forEach(([state, count], index) => {
      if (index) workers.push(span(' · ', 'inactive'));
      workers.push(span(`${count} ${state}`, state === 'running' ? 'suggestion' : state === 'paused' ? 'warning' : 'default'));
    });
  }
  return { unit, lines, workers: pairedLines(workers, finish ? [span(finish, finishTone)] : [], width) };
}

export function BoardView(elements: PaneElements, columns: number, bodyRows: number, snapshot: BoardSnapshot<BoardData>, now: number, select: (production: string, unit: string) => void) {
  const { Box, Text, Button } = elements;
  const width = Math.max(1, Math.floor(columns));
  const limit = Math.max(0, Math.floor(bodyRows));
  const boards = snapshot.boards.map(data => {
    const late = data.units.filter(unit => healthTone(unit, now) === 'error').length;
    const counts = [span(`${data.units.length} units`, 'inactive')];
    if (late) counts.push(span(' · ', 'inactive'), span(`${late} late`, 'error'));
    counts.push(span(` · as of ${clockText(data.as_of.at, data.utc_offset_minutes)}`, 'inactive'));
    const name = [span(data.production, 'default', 'bold')];
    const spacing = width - cellWidth(data.production) - cellWidth(lineText(counts));
    const header = spacing >= 2 ? wrapLine([...name, span(' '.repeat(spacing)), ...counts], width)
      : pairedLines(name, counts, width, 0);
    return { data, header, blocks: [...data.units].sort((a, b) => a.key.localeCompare(b.key, undefined, { sensitivity: 'base' }))
      .map(unit => unitBlock(unit, data, now, width)), hidden: [] as BoardUnit[], showOverflow: true };
  });
  let gap = 1;
  const height = () => boards.reduce((total, board) => total + board.header.length
    + board.blocks.reduce((count, block) => count + block.lines.length + block.workers.length, 0)
    + board.blocks.length * gap + Number(board.showOverflow && board.hidden.length > 0), 0);
  // Line 3 goes first, from the bottom, including all of its continuation rows.
  for (const block of boards.flatMap(board => board.blocks).reverse()) {
    if (height() <= limit) break;
    block.workers = [];
  }
  if (height() > limit) gap = 0;
  for (const board of [...boards].reverse()) {
    while (board.blocks.length && height() > limit) board.hidden.unshift(board.blocks.pop()!.unit);
  }
  // Tiny panes can retain the hidden-unit notice after the production header.
  for (const board of [...boards].reverse()) {
    if (height() > limit) board.header = [];
  }
  for (const board of [...boards].reverse()) {
    if (height() > limit) board.showOverflow = false;
  }
  function drawLine(parts: BoardLine, production: string, key: string, index: number) {
    return <Box flexDirection="row" flexShrink={0}>{parts.filter(part => part.text).map((part, partIndex) => {
      if (part.kind === 'name') return <Button key={index === 0 ? `open-${production}-${key}` : `open-${production}-${key}-${index}-${partIndex}`} plain onPress={() => select(production, key)}><Text bold>{part.text}</Text></Button>;
      if (part.tone === 'default') return <Text bold={part.kind === 'bold'}>{part.text}</Text>;
      return <Text color={part.tone} bold={part.kind === 'bold'}>{part.text}</Text>;
    })}</Box>;
  }
  return <Box flexDirection="column">
    {boards.length === 0 && limit > 0 && wrappedLines('This session is not on the staff list.', width).slice(0, limit).map(line => <Text>{line}</Text>)}
    {boards.map(board => <Box flexDirection="column" flexShrink={0}>
      {board.header.map((line, index) => drawLine(line, board.data.production, '', index))}
      {board.blocks.map(block => <Box flexDirection="column" flexShrink={0} marginTop={gap}>
        {[...block.lines, ...block.workers].map((line, index) => drawLine(line, board.data.production, block.unit.key, index))}
      </Box>)}
      {board.showOverflow && board.hidden.length > 0 && <Text>{doneText(board.hidden.map(unit => unit.key), width, `+${board.hidden.length} more`)}</Text>}
    </Box>)}
  </Box>;
}
