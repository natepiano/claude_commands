import type { Elements } from 'claude-code';

export type PaneElements = Pick<Elements['terminal'], 'Box' | 'Text' | 'Button'>;
import type { BoardData, BoardUnit } from '../types/board';
import type { BoardSnapshot } from '../types';
import { rulerCells, runsOf, signedMinutes, stripCells } from './geometry';

export function etaText(unit: BoardUnit): string {
  const eta = unit.eta;
  if (!eta?.at) return '';
  const history = eta.moved > 0
    ? `first ${eta.first?.text ?? '—'} · moved ${eta.moved}× · ${signedMinutes(eta.delta_minutes ?? 0)} ${(eta.delta_minutes ?? 0) < 0 ? 'early' : 'late'}`
    : 'on its first ETA';
  return `ETA ${eta.at.text}  ${history} · fix rounds ${eta.fix_done}${eta.fix_running ? ' + 1 running' : ''}`;
}

export function gateText(unit: BoardUnit): string {
  const phase = unit.phases.find(row => row.status === 'gated');
  if (!phase || phase.gate.kind === 'none') return '';
  const gate = phase.gate;
  return gate.kind === 'timed'
    ? `Phase ${phase.id} gated until ${gate.until.text} — ${gate.gate}: ${gate.waits_on}`
    : `Phase ${phase.id} gated by ${gate.gate}: ${gate.waits_on}`;
}

export function finishText(unit: BoardUnit): string {
  const finish = unit.finish;
  switch (finish.kind) {
    case 'predicted': return `done by ${finish.at.text}`;
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
  const groups = new Map<string, { role: string; state: string; count: number }>();
  for (const worker of unit.workers) {
    const key = JSON.stringify([worker.role, worker.state]);
    const group = groups.get(key);
    if (group) group.count++;
    else groups.set(key, { role: worker.role, state: worker.state, count: 1 });
  }
  return [...groups.values()].map(group => `${group.count} ${group.role} ${group.state}`).join(' · ');
}

export function BoardView(elements: PaneElements, columns: number, snapshot: BoardSnapshot<BoardData>, now: number, select: (production: string, unit: string) => void) {
  const { Box, Text, Button } = elements;
  const width = Math.max(8, columns - 4);
  return <Box flexDirection="column">
    {snapshot.boards.length === 0 && <Text color="inactive">No running productions.</Text>}
    {snapshot.boards.map(data => <Box flexDirection="column">
      <Box justifyContent="space-between">
        <Text bold>{data.production}</Text>
        <Text color="subtle">{data.units.length} units · as of {data.as_of.text}</Text>
      </Box>
      {data.units.map(unit => <Box flexDirection="column" marginTop={1}>
        <Box justifyContent="space-between">
          <Text wrap="truncate-end"><Text color={unit.state === 'running' ? 'suggestion' : 'inactive'}>● </Text>
            <Text bold>{unit.key}</Text>{unit.phase ? `  Phase ${unit.phase.id}/${unit.phase.of}  ${unit.phase.title}` : '  no phase running'}
          </Text>
          <Button key={`open-${data.production}-${unit.key}`} label="open" plain onPress={() => select(data.production, unit.key)} />
        </Box>
        <Box flexDirection="column" paddingLeft={2}>
          <Text wrap="truncate-end">{runsOf(stripCells(unit.phases)).map(run => <Text color={run.tone}>{run.text}</Text>)}<Text color="subtle">  {unit.stage}</Text></Text>
          {etaText(unit) && <Text wrap="truncate-end">{etaText(unit)}</Text>}
          {gateText(unit) && <Text color="warning" wrap="wrap">{gateText(unit)}</Text>}
          {finishText(unit) && <Text color="subtle">{finishText(unit)}</Text>}
          {RulerView(elements, unit, data, now, width)}
          {unit.workers.length > 0 && <Text color="subtle" wrap="truncate-end">workers  {workersText(unit)}</Text>}
        </Box>
      </Box>)}
    </Box>)}
  </Box>;
}
