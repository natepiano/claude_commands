import type { BoardPhase, BoardUnit } from '../types/board';

export type Tone = 'success' | 'error' | 'warning' | 'suggestion' | 'inactive' | 'subtle';
export interface Cell { ch: string; tone: Tone }
export interface Run { text: string; tone: Tone }
export interface Ruler { track: Cell[]; labels: string; from: number; to: number }

export function runsOf(cells: readonly Cell[]): Run[] {
  const runs: Run[] = [];
  for (const cell of cells) {
    const last = runs[runs.length - 1];
    if (last?.tone === cell.tone) last.text += cell.ch;
    else runs.push({ text: cell.ch, tone: cell.tone });
  }
  return runs;
}

export function stripCells(phases: readonly BoardPhase[], room = 30): Cell[] {
  const done = phases.filter(phase => phase.status === 'done').length;
  if (phases.length > 30 || phases.length > room) {
    return Array.from(`${done}/${phases.length} done`, ch => ({ ch, tone: 'subtle' }));
  }
  const styles: Record<BoardPhase['status'], Cell> = {
    done: { ch: '█', tone: 'success' }, open: { ch: '▓', tone: 'suggestion' },
    todo: { ch: '░', tone: 'inactive' }, gated: { ch: '▒', tone: 'warning' },
  };
  return phases.map(phase => ({ ...styles[phase.status] }));
}

export function clockText(epoch: number, offset: number): string {
  const local = new Date((epoch + offset * 60) * 1000);
  return `${String(local.getUTCHours()).padStart(2, '0')}:${String(local.getUTCMinutes()).padStart(2, '0')}`;
}

/** Later marks displace entire earlier labels, never pieces of them. */
export function labelRuns(marks: readonly { column: number; text: string }[], width: number): string {
  const placed: { start: number; text: string }[] = [];
  for (const mark of marks) {
    if (mark.text.length > width) continue;
    const start = Math.max(0, Math.min(width - mark.text.length, mark.column));
    for (let index = placed.length - 1; index >= 0; index--) {
      const old = placed[index]!;
      if (start <= old.start + old.text.length && old.start <= start + mark.text.length) placed.splice(index, 1);
    }
    placed.push({ start, text: mark.text });
  }
  const cells = Array.from({ length: width }, () => ' ');
  for (const mark of placed) Array.from(mark.text).forEach((ch, index) => { cells[mark.start + index] = ch; });
  return cells.join('').trimEnd();
}

export function rulerCells(unit: BoardUnit, now: number, width: number, utcOffsetMinutes = 0): Ruler {
  width = Math.max(1, Math.floor(width));
  const phase = unit.phases.find(row => row.id === unit.phase?.id);
  const from = phase?.start?.at ?? now;
  const promise = unit.eta?.at;
  const end = Math.max(promise?.at ?? now, now, from + 1);
  const to = end + (end - from) * 0.1;
  const column = (at: number) => Math.max(0, Math.min(width - 1, Math.round((at - from) / (to - from) * (width - 1))));
  const track: Cell[] = Array.from({ length: width }, (_, index) => ({
    ch: index <= column(now) ? '━' : '─', tone: index <= column(now) ? 'suggestion' : 'subtle',
  }));
  const marks = [{ column: 0, text: phase?.start?.text ?? clockText(from, utcOffsetMinutes) }];
  for (const [target] of unit.eta?.trail ?? []) {
    track[column(target.at)] = { ch: '○', tone: 'inactive' };
    marks.push({ column: column(target.at), text: target.text });
  }
  if (promise) {
    track[column(promise.at)] = { ch: '●', tone: 'suggestion' };
    marks.push({ column: column(promise.at), text: promise.text });
  }
  track[column(now)] = { ch: '│', tone: 'error' };
  marks.push({ column: column(now), text: clockText(now, utcOffsetMinutes) });
  return { track, labels: labelRuns(marks, width), from, to };
}

export function signedMinutes(minutes: number): string {
  const size = Math.round(Math.abs(minutes));
  return `${minutes < 0 ? '-' : '+'}${Math.floor(size / 60)}:${String(size % 60).padStart(2, '0')}`;
}

export function duration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}`;
}
