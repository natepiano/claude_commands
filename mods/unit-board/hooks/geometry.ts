import type { BoardPhase, BoardUnit } from '../types/board';

export type Tone = 'success' | 'error' | 'warning' | 'suggestion' | 'inactive' | 'subtle';
export interface Cell { ch: string; tone: Tone }
export interface Run { text: string; tone: Tone }
export interface Ruler { track: Cell[]; labels: string; from: number; to: number }

/** Terminal cells occupied by ordinary, combining, wide and emoji characters. */
export function cellWidth(text: string): number {
  let width = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0)!;
    if (/[\p{Mark}\p{Cf}]/u.test(ch) || code < 0x20 || (code >= 0x7f && code < 0xa0)) continue;
    const wide = code >= 0x1100 && (code <= 0x115f || code === 0x2329 || code === 0x232a
      || (code >= 0x2e80 && code <= 0xa4cf && code !== 0x303f)
      || (code >= 0xac00 && code <= 0xd7a3) || (code >= 0xf900 && code <= 0xfaff)
      || (code >= 0xfe10 && code <= 0xfe19) || (code >= 0xfe30 && code <= 0xfe6f)
      || (code >= 0xff01 && code <= 0xff60) || (code >= 0xffe0 && code <= 0xffe6)
      || (code >= 0x1b000 && code <= 0x1b2ff) || (code >= 0x20000 && code <= 0x3fffd));
    width += wide || /[\p{Emoji_Presentation}\p{Extended_Pictographic}]/u.test(ch) ? 2 : 1;
  }
  return width;
}

/** Keep the following column's space even when a title fills its entire cell. */
export function fitText(text: string, width: number): string {
  text = text.replace(/\s+/gu, ' ');
  width = Math.max(0, Math.floor(width));
  if (cellWidth(text) <= width) return text;
  if (!width) return '';
  let prefix = '';
  let used = 0;
  for (const ch of text) {
    const size = cellWidth(ch);
    if (used + size > width - 1) break;
    prefix += ch;
    used += size;
  }
  return prefix + '…';
}

export function doneText(keys: readonly string[], width: number, label = 'Done'): string {
  for (let count = keys.length; count >= 0; count--) {
    const names = keys.slice(0, count).join(', ');
    const more = count < keys.length ? `${names ? ', ' : ''}+${keys.length - count} more` : '';
    const line = `${label}: ${names}${more}`;
    if (cellWidth(line) <= width) return line;
  }
  return fitText(`${label}: +${keys.length} more`, width);
}

export function overdueText(at: number, now: number): string {
  if (now <= at) return '';
  const minutes = Math.floor((now - at) / 60);
  const hours = Math.floor(minutes / 60);
  const elapsed = hours >= 24 ? `${Math.floor(hours / 24)}d ${hours % 24}h`
    : hours > 0 ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
  return ` · overdue ${elapsed}`;
}

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
  const phase = unit.phases.find(row => row.id === unit.phase?.id && row.status === 'open');
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
