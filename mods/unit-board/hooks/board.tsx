import type { Elements } from 'claude-code';

export type PaneElements = Pick<Elements['terminal'], 'Box' | 'Text' | 'Button'>;
import type { BoardData, BoardUnit } from '../types/board';
import type { BoardSnapshot } from '../types';
import { cellWidth, doneText, fitText, overdueText, rulerCells, runsOf, signedMinutes, stripCells } from './geometry';

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

export function RulerView(elements: PaneElements, unit: BoardUnit, data: BoardData, now: number, width: number) {
  const { Box, Text } = elements;
  if (!unit.phase) return <Box />;
  const ruler = rulerCells(unit, now, width, data.utc_offset_minutes);
  return <Box flexDirection="column">
    <Text wrap="truncate-end">{runsOf(ruler.track).map(run => <Text color={run.tone}>{run.text}</Text>)}</Text>
    <Text color="subtle" wrap="truncate-end">{ruler.labels}</Text>
  </Box>;
}

export function workersText(unit: BoardUnit): string {
  const groups = new Map<string, { role: string; state: string; count: number; showCount: boolean }>();
  for (const worker of unit.workers) {
    if (worker.state === 'ended') continue;
    const role = worker.role || worker.slot;
    const key = JSON.stringify([worker.role, role, worker.state]);
    const group = groups.get(key);
    if (group) group.count++;
    else groups.set(key, { role, state: worker.state, count: 1, showCount: Boolean(worker.role) });
  }
  return [...groups.values()].map(group => [group.showCount || group.count > 1 ? group.count : '', group.role, group.state].filter(Boolean).join(' ')).join(' · ');
}

/** A visible unit's full detail rows, before the pane's height budget is applied. */
interface UnitBlock {
  unit: BoardUnit;
  strip: ReturnType<typeof stripCells>;
  eta: string;
  gate: string;
  finish: string;
  workers: string;
  detailRows: number;
  compact: boolean;
}

function foldLabel(unit: BoardUnit): string {
  if (unit.phase) return '';
  if (unit.state === 'run-finished') return 'Done';
  return unit.state === 'standing-by' && unit.stage === 'Standing by' ? 'Standing by' : '';
}

function unitBlock(unit: BoardUnit, now: number): UnitBlock {
  const strip = stripCells(unit.phases);
  const showsDetails = unit.state === 'running' || Boolean(unit.phase);
  const eta = showsDetails ? etaText(unit, now) : '';
  const gate = showsDetails ? gateText(unit) : '';
  const finish = showsDetails && (unit.state === 'run-finished' || unit.phase || unit.phases.some(row => row.status === 'todo' || row.status === 'gated'))
    ? finishText(unit, now) : '';
  const workers = showsDetails ? workersText(unit) : '';
  const detailRows = Number(strip.length > 0 || Boolean(unit.phase))
    + Number(Boolean(eta)) + Number(Boolean(gate)) + Number(Boolean(finish))
    + (unit.phase ? 2 : 0) + Number(Boolean(workers));
  return { unit, strip, eta, gate, finish, workers, detailRows, compact: false };
}

export function BoardView(elements: PaneElements, columns: number, bodyRows: number, snapshot: BoardSnapshot<BoardData>, now: number, select: (production: string, unit: string) => void) {
  const { Box, Text, Button } = elements;
  const width = Math.max(0, columns - 4);
  const boards = snapshot.boards.map(data => ({
    data,
    folds: ['Done', 'Standing by'].flatMap(label => {
      const keys = data.units.filter(unit => foldLabel(unit) === label).map(unit => unit.key);
      return keys.length ? [doneText(keys, width, label)] : [];
    }),
    blocks: data.units.filter(unit => !foldLabel(unit)).sort((left, right) => {
      const order = (unit: BoardUnit) => unit.state === 'running' ? 0 : unit.state === 'run-finished' ? 2 : 1;
      return order(left) - order(right);
    }).map(unit => unitBlock(unit, now)),
    hidden: [] as BoardUnit[],
    showHeader: true,
    showOverflow: true,
  }));
  const blocks = boards.flatMap(board => board.blocks);
  let gap = 1;
  let rows = boards.reduce((total, board) => total + 1 + board.folds.length, 0)
    + blocks.reduce((total, block) => total + 1 + block.detailRows + gap, 0);
  // Preserve the top units' full details longest; reserve both fold lines first.
  for (let index = blocks.length - 1; index >= 0 && rows > bodyRows; index--) {
    const block = blocks[index]!;
    if (block.unit.state !== 'running' || block.detailRows <= 1) continue;
    block.compact = true;
    rows -= block.detailRows - Number(Boolean(block.unit.phase) || block.strip.length > 0);
  }
  if (rows > bodyRows) {
    rows -= blocks.length * gap;
    gap = 0;
  }
  // At minimum block height, keep a prefix of whole units and name the rest.
  for (let index = boards.length - 1; index >= 0 && rows > bodyRows; index--) {
    const board = boards[index]!;
    while (board.blocks.length && rows > bodyRows) {
      const block = board.blocks.pop()!;
      rows -= 1 + (block.compact ? Number(Boolean(block.unit.phase) || block.strip.length > 0) : block.detailRows);
      if (!board.hidden.length) rows++;
      board.hidden.unshift(block.unit);
    }
  }
  // A very short pane may hold only the fold lines, without any board chrome.
  for (let index = boards.length - 1; index >= 0 && rows > bodyRows; index--) {
    boards[index]!.showHeader = false;
    rows--;
  }
  for (let index = boards.length - 1; index >= 0 && rows > bodyRows; index--) {
    const board = boards[index]!;
    if (!board.hidden.length) continue;
    board.showOverflow = false;
    rows--;
  }
  for (let index = boards.length - 1; index >= 0 && rows > bodyRows; index--) {
    const board = boards[index]!;
    while (board.folds.length && rows > bodyRows) {
      board.folds.pop();
      rows--;
    }
  }
  return <Box flexDirection="column">
    {boards.length === 0 && bodyRows > 0 && <Text color="inactive">No running productions.</Text>}
    {boards.map(({ data, folds, blocks, hidden, showHeader, showOverflow }) => <Box flexDirection="column" flexShrink={0}>
      {showHeader && <Box justifyContent="space-between">
        <Text bold wrap="truncate-end">{data.production}</Text>
        <Text color="subtle" wrap="truncate-end">{data.units.length} units · as of {data.as_of.text}</Text>
      </Box>}
      {blocks.map(({ unit, strip, eta, gate, finish, workers, detailRows, compact }) => {
        const heading = unit.phase ? `  Phase ${unit.phase.id}/${unit.phase.of}  ${unit.phase.title}` : `  ${unit.stage}`;
        return <Box flexDirection="column" flexShrink={0} marginTop={gap}>
          <Box justifyContent="space-between">
            <Text wrap="truncate-end"><Text color={unit.state === 'running' ? 'suggestion' : 'inactive'}>● </Text>
              <Text bold>{unit.key}</Text>{fitText(heading, Math.max(0, columns - cellWidth(unit.key) - 10))}{'  '}
            </Text>
            <Button key={`open-${data.production}-${unit.key}`} label="open" plain onPress={() => select(data.production, unit.key)} />
          </Box>
          {compact ? (unit.phase || strip.length > 0) && <Text wrap="truncate-end">{'  '}{unit.phase && <Text>{unit.stage}{unit.eta?.at ? `  ETA ${unit.eta.at.text}${overdueText(unit.eta.at.at, now)}` : ''}{strip.length ? '  ' : ''}</Text>}{runsOf(strip).map(run => <Text color={run.tone}>{run.text}</Text>)}</Text>
            : detailRows > 0 && <Box flexDirection="column" paddingLeft={2}>
              {(strip.length > 0 || unit.phase) && <Text wrap="truncate-end">{runsOf(strip).map(run => <Text color={run.tone}>{run.text}</Text>)}{unit.phase && <Text color="subtle">{strip.length > 0 ? '  ' : ''}{unit.stage}</Text>}</Text>}
              {eta.length > 0 && <Text wrap="truncate-end">{eta}</Text>}
              {gate.length > 0 && <Text color="warning" wrap="truncate-end">{gate}</Text>}
              {finish.length > 0 && <Text color="subtle" wrap="truncate-end">{finish}</Text>}
              {unit.phase && RulerView(elements, unit, data, now, width)}
              {workers.length > 0 && <Text color="subtle" wrap="truncate-end">workers  {workers}</Text>}
            </Box>}
        </Box>;
      })}
      {showOverflow && hidden.length > 0 && <Text color="subtle" wrap="truncate-end">{doneText(hidden.map(unit => unit.key), width,
        `+${hidden.length} more ${hidden.every(unit => unit.state === 'running') ? 'running' : 'units'}`)}</Text>}
      {folds.map(line => <Text color="subtle" wrap="truncate-end">{line}</Text>)}
    </Box>)}
  </Box>;
}
