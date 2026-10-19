import type { RenderElement } from 'claude-code';
import type { PaneElements } from './board';
import type { Tone } from './geometry';
import type { BoardData, BoardPhase, BoardUnit } from '../types/board';
import { cellWidth, duration, fitText, runsOf, signedMinutes, stripCells, wrappedLines } from './geometry';
import { etaText, finishText, gateText, RulerView } from './board';

function recordText(phase: BoardPhase): string {
  const eta = phase.eta;
  if (eta.delta_minutes !== null) {
    return `${signedMinutes(eta.delta_minutes)} ${eta.delta_minutes > 0 ? 'late' : 'early'}, said ${eta.trail.at(-1)?.[0].text ?? '—'}`;
  }
  return eta.trail.length ? `ETA ${eta.trail.at(-1)?.[0].text}` : 'none stated';
}

/** Rows are removed in this order only after history and steps have folded. */
type UnitRowRetention = 'context' | 'worker' | 'step' | 'fold' | 'phase' | 'heading' | 'open-phase' | 'navigation';
interface UnitRow { view: RenderElement; height: number; retention: UnitRowRetention }
interface UnitSection { rows: UnitRow[]; gap: number }

function sectionHeight(sections: readonly UnitSection[]): number {
  return sections.reduce((total, section) => total + (section.rows.length
    ? section.gap + section.rows.reduce((height, row) => height + row.height, 0) : 0), 0);
}

function earlierIds(phases: readonly BoardPhase[]): string {
  const ids = phases.map(phase => phase.id);
  const consecutive = ids.every((id, index) => id === String(Number(ids[0]) + index));
  return ids.length > 1 && consecutive ? `${ids[0]}–${ids.at(-1)}` : ids.join(', ');
}

function padCells(text: string, width: number): string {
  return text + ' '.repeat(Math.max(0, width - cellWidth(text)));
}

export function UnitView(elements: PaneElements, columns: number, bodyRows: number, data: BoardData, unit: BoardUnit, now: number, back: () => void) {
  const { Box, Text, Button } = elements;
  const limit = Math.max(0, Math.floor(bodyRows));
  const fact = (text: string, tone: Tone = 'default') => tone === 'default'
    ? <Text>{text}</Text> : <Text color={tone}>{text}</Text>;
  const eta = etaText(unit, now);
  const gate = gateText(unit);
  const finish = finishText(unit, now);
  const heading = !unit.phase ? unit.stage : unit.phase.kind === 'ticket'
    ? `Ticket ${unit.phase.id} · ${unit.phase.merged} merged · ${unit.stage}`
    : `Phase ${unit.phase.id} of ${unit.phase.of} · ${unit.stage}`;
  const trail = unit.eta?.trail.map(([eta, stated]) => `${eta.text} said ${stated.text}`).join(' → ') ?? '';
  const strip = stripCells(unit.phases);
  const slotWidth = Math.max(0, ...unit.workers.map(worker => cellWidth(worker.slot))) + 1;
  const roleWidth = Math.max(0, ...unit.workers.map(worker => cellWidth(worker.role || worker.slot))) + 1;
  const stateWidth = Math.max(0, ...unit.workers.map(worker => cellWidth(worker.state))) + 1;
  const idWidth = Math.max(3, ...unit.phases.map(phase => cellWidth(phase.id)));
  const fixWidth = Math.max(0, ...unit.phases.map(phase => phase.fix_done + (phase.fix_running ? 1 : 0))) + 1;
  const timingWidth = Math.max(7, ...unit.phases.map(phase => (phase.took === null ? phase.status : duration(phase.took)).length + 1));
  const recordWidth = Math.max(0, ...unit.phases.map(phase => cellWidth(recordText(phase))));
  const stepTimes = unit.steps.map(step => `${step[2].text} · ${duration(step[3])}`);
  const markWidth = Math.max(cellWidth('✓ '), cellWidth('▶ '));
  const timeWidth = Math.min(Math.max(0, columns - markWidth - 2), Math.max(0, ...stepTimes.map(cellWidth)));
  const labelWidth = Math.max(0, columns - timeWidth - markWidth - 2);
  const done = unit.phases.filter(phase => phase.status === 'done');
  const future = unit.phases.filter(phase => phase.status === 'todo' || phase.status === 'gated');
  const hiddenDone = new Set<BoardPhase>();
  const hiddenFuture = new Set<BoardPhase>();
  let hiddenSteps = 0;
  const row = (view: RenderElement, retention: UnitRowRetention = 'context', height = 1): UnitRow => ({ view, height, retention });

  function sections(): UnitSection[] {
    const overview = [row(<Box justifyContent="space-between">
      <Text wrap="truncate-end"><Text bold>{unit.key}</Text>  {heading}</Text>
      <Button key="back" label="board" plain onPress={back} />
    </Box>, 'navigation')];
    if (unit.phase?.title) overview.push(row(<Text bold wrap="truncate-end">{unit.phase.title}</Text>));
    if (unit.plan) overview.push(row(unit.phase
      ? <Text wrap="truncate-end">{unit.plan}</Text>
      : <Text bold wrap="truncate-end">{unit.plan}</Text>));
    overview.push(...wrappedLines(unit.summary, columns).map(line => row(<Text wrap="truncate-end">{line}</Text>)));
    overview.push(row(<Text wrap="truncate-end"><Text color="inactive">as of </Text>{data.as_of.text}</Text>));
    const timing: UnitRow[] = [];
    timing.push(...wrappedLines(eta, columns).map(line => row(<Text wrap="truncate-end">{line}</Text>)));
    timing.push(...wrappedLines(gate, columns).map(line => row(<Text color="warning" wrap="truncate-end">{line}</Text>)));
    timing.push(...wrappedLines(finish, columns).map(line => row(<Text wrap="truncate-end">{line}</Text>)));
    if (unit.phase) timing.push(row(RulerView(elements, unit, data, now, Math.max(1, columns - 2)), 'context', 2));
    timing.push(...wrappedLines(trail, columns).map(line => row(<Text wrap="truncate-end">{line}</Text>)));
    const steps: UnitRow[] = [];
    if (unit.steps.length) {
      steps.push(row(<Text bold>This phase</Text>, 'heading'));
      if (hiddenSteps) steps.push(row(<Text color="inactive" wrap="truncate-end">+{hiddenSteps} earlier steps</Text>, 'fold'));
      unit.steps.slice(hiddenSteps).forEach(([state, what], offset) => {
        const label = fitText(what, labelWidth);
        steps.push(row(<Text wrap="truncate-end"><Text color={state === 'running' ? 'suggestion' : 'success'}>{padCells(state === 'running' ? '▶ ' : '✓ ', markWidth)}</Text>{padCells(label, labelWidth)}  <Text>{fitText(stepTimes[hiddenSteps + offset]!, timeWidth)}</Text></Text>, 'step'));
      });
    }
    const workers: UnitRow[] = unit.workers.length ? [row(<Text bold>Workers</Text>, 'heading'), ...unit.workers.map(worker => row(
      <Text wrap="truncate-end"><Text>{padCells(worker.slot, slotWidth)}</Text><Text bold>{padCells(worker.role || worker.slot, roleWidth)}</Text>{fact(padCells(worker.state, stateWidth), worker.state === 'running' ? 'suggestion' : worker.state === 'paused' ? 'warning' : 'default')}{worker.doing}</Text>, 'worker'))] : [];
    const record = [row(<Text bold>Phase record</Text>, 'heading')];
    if (!unit.phases.length) record.push(row(<Text color="inactive">no phases recorded</Text>, 'phase'));
    if (hiddenFuture.size) record.push(row(<Text color="inactive" wrap="truncate-end">+{hiddenFuture.size} later</Text>, 'fold'));
    [...unit.phases].reverse().filter(phase => !hiddenDone.has(phase) && !hiddenFuture.has(phase)).forEach(phase => {
      const id = ' '.repeat(Math.max(0, idWidth - cellWidth(phase.id))) + phase.id;
      record.push(row(<Text wrap="truncate-end">
        <Text>{id}  </Text>{(phase.took === null ? phase.status : duration(phase.took)).padEnd(timingWidth)}
        <Text color="warning">{('●'.repeat(phase.fix_done) + (phase.fix_running ? '○' : '')).padEnd(fixWidth)}</Text>
        {fact(padCells(recordText(phase), recordWidth), phase.eta.delta_minutes !== null && phase.eta.delta_minutes > 0 ? 'error' : 'default')}  {phase.title}
      </Text>, phase.status === 'open' ? 'open-phase' : 'phase'));
    });
    if (hiddenDone.size) record.push(row(<Text color="inactive" wrap="truncate-end">{fitText(`+${hiddenDone.size} earlier: ${earlierIds(done.filter(phase => hiddenDone.has(phase)))}`, columns)}</Text>, 'fold'));
    return [{ rows: overview, gap: 0 }, { rows: timing, gap: 1 },
      { rows: strip.length ? [row(<Text wrap="truncate-end">{runsOf(strip).map(run => run.tone === 'default' ? <Text>{run.text}</Text> : <Text color={run.tone}>{run.text}</Text>)}</Text>)] : [], gap: 1 },
      { rows: steps, gap: 1 }, { rows: workers, gap: 1 }, { rows: record, gap: 1 }];
  }

  let content = sections();
  if (sectionHeight(content) > limit || future.length > 3) {
    // A useful fold replaces at least two old rows with one notice.
    future.slice(3).forEach(phase => hiddenFuture.add(phase));
    if (done.length > 2) done.slice(0, 2).forEach(phase => hiddenDone.add(phase));
    if (unit.steps.length > 2) hiddenSteps = 2;
    content = sections();
    for (const phase of done.slice(hiddenDone.size, -1)) {
      if (sectionHeight(content) <= limit) break;
      hiddenDone.add(phase);
      content = sections();
    }
    while (sectionHeight(content) > limit && unit.steps.length - hiddenSteps > 1) {
      hiddenSteps++;
      content = sections();
    }
  }
  if (sectionHeight(content) > limit) content.forEach(section => { section.gap = 0; });
  // Extremely short panes retain navigation and the open record row longest.
  for (const retention of ['context', 'worker', 'step', 'fold', 'phase', 'open-phase', 'navigation'] as const) {
    for (const section of content) {
      for (let index = section.rows.length - 1; index >= 0 && sectionHeight(content) > limit; index--) {
        if (section.rows[index]!.retention === retention) section.rows.splice(index, 1);
        if (section.rows.every(row => row.retention === 'heading')) {
          section.rows = [];
          break;
        }
      }
    }
  }
  return <Box flexDirection="column">{content.filter(section => section.rows.length).map(section =>
    <Box flexDirection="column" flexShrink={0} marginTop={section.gap}>{section.rows.map(row => row.view)}</Box>)}</Box>;
}
