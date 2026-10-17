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
  const eta = etaText(unit, now);
  const gate = gateText(unit);
  const finish = finishText(unit, now);
  const trail = unit.eta?.trail.map(([eta, stated]) => `${eta.text} said ${stated.text}`).join(' → ') ?? '';
  const strip = stripCells(unit.phases);
  const slotWidth = Math.max(0, ...unit.workers.map(worker => worker.slot.length)) + 1;
  const roleWidth = Math.max(0, ...unit.workers.map(worker => (worker.role || worker.slot).length)) + 1;
  const stateWidth = Math.max(0, ...unit.workers.map(worker => worker.state.length)) + 1;
  const idWidth = Math.max(3, ...unit.phases.map(phase => phase.id.length));
  const fixWidth = Math.max(0, ...unit.phases.map(phase => phase.fix_done + (phase.fix_running ? 1 : 0))) + 1;
  const timingWidth = Math.max(7, ...unit.phases.map(phase => (phase.took === null ? phase.status : duration(phase.took)).length + 1));
  const recordWidth = Math.max(0, ...unit.phases.map(phase => recordText(phase).length));
  return <Box flexDirection="column">
    <Box justifyContent="space-between">
      <Text wrap="truncate-end"><Text bold>{unit.key}</Text>  {unit.phase ? `Phase ${unit.phase.id} of ${unit.phase.of} · ` : ''}{unit.stage}</Text>
      <Button key="back" label="board" plain onPress={back} />
    </Box>
    {Boolean(unit.phase?.title || unit.plan) && <Text bold wrap="truncate-end">{unit.phase?.title || unit.plan}</Text>}
    {unit.phase && unit.plan.length > 0 && <Text color="subtle" wrap="truncate-end">{unit.plan}</Text>}
    {unit.summary.length > 0 && <Text color="subtle" wrap="wrap">{unit.summary}</Text>}
    <Text color="subtle">as of {data.as_of.text}</Text>
    {Boolean(eta || gate || finish || unit.phase || trail) && <Box flexDirection="column" marginTop={1}>
      {eta.length > 0 && <Text wrap="wrap">{eta}</Text>}
      {gate.length > 0 && <Text color="warning" wrap="wrap">{gate}</Text>}
      {finish.length > 0 && <Text color="subtle">{finish}</Text>}
      {unit.phase && RulerView(elements, unit, data, now, Math.max(8, columns - 2))}
      {trail.length > 0 && <Text color="subtle" wrap="wrap">{trail}</Text>}
    </Box>}
    {strip.length > 0 && <Box marginTop={1}><Text>{runsOf(strip).map(run => <Text color={run.tone}>{run.text}</Text>)}</Text></Box>}
    {unit.steps.length > 0 && <Box flexDirection="column" marginTop={1}>
      <Text bold>This phase</Text>
      {unit.steps.map(([state, what, started, took]) => <Box justifyContent="space-between">
        <Text wrap="truncate-end"><Text color={state === 'running' ? 'suggestion' : 'success'}>{state === 'running' ? '▶' : '✓'} </Text>{what}</Text>
        <Text color="subtle">{started.text} · {duration(took)}</Text>
      </Box>)}
    </Box>}
    {unit.workers.length > 0 && <Box flexDirection="column" marginTop={1}>
      <Text bold>Workers</Text>
      {unit.workers.map(worker => <Text wrap="truncate-end"><Text color="subtle">{worker.slot.padEnd(slotWidth)}</Text><Text bold>{(worker.role || worker.slot).padEnd(roleWidth)}</Text><Text color={worker.state === 'running' ? 'suggestion' : 'inactive'}>{worker.state.padEnd(stateWidth)}</Text>{worker.doing}</Text>)}
    </Box>}
    <Box flexDirection="column" marginTop={1}>
      <Text bold>Phase record</Text>
      {unit.phases.length === 0 && <Text color="subtle">no phases recorded</Text>}
      {[...unit.phases].reverse().map(phase => <Text wrap="truncate-end">
        <Text color="subtle">{phase.id.padStart(idWidth)}  </Text>{(phase.took === null ? phase.status : duration(phase.took)).padEnd(timingWidth)}
        <Text color="warning">{('●'.repeat(phase.fix_done) + (phase.fix_running ? '○' : '')).padEnd(fixWidth)}</Text>
        <Text color={phase.eta.delta_minutes !== null && phase.eta.delta_minutes > 0 ? 'error' : 'subtle'}>{recordText(phase).padEnd(recordWidth)}</Text>  {phase.title}
      </Text>)}
    </Box>
  </Box>;
}
