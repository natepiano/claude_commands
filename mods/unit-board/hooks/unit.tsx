import type { PaneElements } from './board';
import type { BoardData, BoardPhase, BoardUnit } from '../types/board';
import { duration, runsOf, signedMinutes, stripCells } from './geometry';
import { etaText, finishText, gateText, RulerView } from './board';

function recordText(phase: BoardPhase): string {
  const eta = phase.eta;
  if (eta.delta_minutes !== null) {
    return `${signedMinutes(eta.delta_minutes)} ${eta.delta_minutes > 0 ? 'late' : 'early'}, said ${eta.trail.at(-1)?.[0].text ?? '—'}`;
  }
  return eta.trail.length ? `ETA ${eta.trail.at(-1)?.[0].text}` : 'none stated';
}

export function UnitView(elements: PaneElements, columns: number, data: BoardData, unit: BoardUnit, now: number, back: () => void) {
  const { Box, Text, Button } = elements;
  return <Box flexDirection="column">
    <Box justifyContent="space-between">
      <Text wrap="truncate-end"><Text bold>{unit.key}</Text>  {unit.phase ? `Phase ${unit.phase.id} of ${unit.phase.of}` : 'No phase running'} · {unit.stage}</Text>
      <Button key="back" label="board" plain onPress={back} />
    </Box>
    <Text bold wrap="truncate-end">{unit.phase?.title ?? unit.plan}</Text>
    <Text color="subtle" wrap="wrap">{unit.summary}</Text>
    <Text color="subtle">as of {data.as_of.text}</Text>
    <Box flexDirection="column" marginTop={1}>
      {etaText(unit) && <Text wrap="wrap">{etaText(unit)}</Text>}
      {gateText(unit) && <Text color="warning" wrap="wrap">{gateText(unit)}</Text>}
      {finishText(unit) && <Text color="subtle">{finishText(unit)}</Text>}
      {RulerView(elements, unit, data, now, Math.max(8, columns - 2))}
      {unit.eta && <Text color="subtle" wrap="wrap">{unit.eta.trail.map(([eta, stated]) => `${eta.text} said ${stated.text}`).join(' → ')}</Text>}
    </Box>
    <Box marginTop={1}><Text>{runsOf(stripCells(unit.phases)).map(run => <Text color={run.tone}>{run.text}</Text>)}</Text></Box>
    {unit.steps.length > 0 && <Box flexDirection="column" marginTop={1}>
      <Text bold>This phase</Text>
      {unit.steps.map(([state, what, started, took]) => <Box justifyContent="space-between">
        <Text wrap="truncate-end"><Text color={state === 'running' ? 'suggestion' : 'success'}>{state === 'running' ? '▶' : '✓'} </Text>{what}</Text>
        <Text color="subtle">{started.text} · {duration(took)}</Text>
      </Box>)}
    </Box>}
    {unit.workers.length > 0 && <Box flexDirection="column" marginTop={1}>
      <Text bold>Workers</Text>
      {unit.workers.map(worker => <Text wrap="truncate-end"><Text color="subtle">{worker.slot} </Text><Text bold>{worker.role.padEnd(7)}</Text><Text color={worker.state === 'running' ? 'suggestion' : 'inactive'}>{worker.state.padEnd(8)}</Text>{worker.doing}</Text>)}
    </Box>}
    <Box flexDirection="column" marginTop={1}>
      <Text bold>Phase record</Text>
      {[...unit.phases].reverse().map(phase => <Text wrap="truncate-end">
        <Text color="subtle">{phase.id.padStart(3)}  </Text>{(phase.took === null ? phase.status : duration(phase.took)).padEnd(7)}
        <Text color="warning">{'●'.repeat(phase.fix_done)}{phase.fix_running ? '○' : ''} </Text>
        <Text color={phase.eta.delta_minutes !== null && phase.eta.delta_minutes > 0 ? 'error' : 'subtle'}>{recordText(phase)}</Text>  {phase.title}
      </Text>)}
    </Box>
  </Box>;
}
