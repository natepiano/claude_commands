import type { BoardPhase, BoardTime, BoardUnit } from '../types/board';

export type Tone = 'success' | 'error' | 'warning' | 'suggestion' | 'inactive' | 'default';
export interface Cell { ch: string; tone: Tone }
export interface Run { text: string; tone: Tone }
export interface Ruler { track: Cell[]; labels: string; labelCells: Cell[]; from: number; to: number }

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
  text = text.replace(/[ \t]*[\r\n\t][\s]*/gu, ' ');
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
  return wrappedLines(`${label}: +${keys.length} more`, width)[0] ?? '';
}

export function overdueText(at: number, now: number): string {
  if (now <= at) return '';
  return ` · overdue ${elapsedText(now - at)}`;
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

/** Every cell summarizes a contiguous slice, with unfinished work taking priority. */
export function stripCells(phases: readonly BoardPhase[]): Cell[] {
  const styles: Record<BoardPhase['status'], Cell> = {
    done: { ch: '█', tone: 'success' }, open: { ch: '▓', tone: 'suggestion' },
    todo: { ch: '░', tone: 'inactive' }, gated: { ch: '▒', tone: 'warning' },
  };
  const count = Math.min(20, phases.length);
  return Array.from({ length: count }, (_, index) => {
    const slice = phases.slice(Math.floor(index * phases.length / count), Math.floor((index + 1) * phases.length / count));
    const status = (['open', 'gated', 'todo', 'done'] as const).find(state => slice.some(phase => phase.status === state))!;
    return { ...styles[status] };
  });
}

/** Explicit wrapping makes each rendered row's height known before folding. */
export function wrappedLines(text: string, columns: number, continuationIndent = 0): string[] {
  const width = Math.max(1, Math.floor(columns));
  const indent = ' '.repeat(Math.max(0, Math.min(width - 1, continuationIndent)));
  return text.replace(/\r\n?/gu, '\n').split('\n').flatMap(paragraph => {
    const lines: string[] = [];
    let rest = paragraph;
    while (rest.length) {
      const prefix = lines.length ? indent : '';
      const room = width - cellWidth(prefix);
      if (cellWidth(rest) <= room) {
        lines.push(prefix + rest);
        break;
      }
      let end = 0;
      let used = 0;
      for (const ch of rest) {
        if (used + cellWidth(ch) > room) break;
        end += ch.length;
        used += cellWidth(ch);
      }
      // A wide glyph cannot occupy a single remaining cell. Give it a full
      // row when possible, rather than replacing it with an ellipsis.
      if (!end) {
        const ch = Array.from(rest)[0]!;
        if (cellWidth(ch) <= width) lines.push(ch);
        rest = rest.slice(ch.length);
        continue;
      }
      const space = rest.slice(0, end).lastIndexOf(' ');
      if (space > 0) end = space;
      lines.push(prefix + rest.slice(0, end).trimEnd());
      rest = rest.slice(end).trimStart();
    }
    return lines;
  });
}

/** Calendar comparisons use the production offset, including across midnight. */
export function shortTime(time: BoardTime, now: number, utcOffsetMinutes = 0): string {
  const local = new Date((time.at + utcOffsetMinutes * 60) * 1000);
  const day = Math.floor((time.at + utcOffsetMinutes * 60) / 86400);
  const today = Math.floor((now + utcOffsetMinutes * 60) / 86400);
  const clock = clockText(time.at, utcOffsetMinutes);
  if (day === today) return clock;
  if (Math.abs(day - today) <= 6) return `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][local.getUTCDay()]} ${clock}`;
  return `${local.getUTCDate()} ${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][local.getUTCMonth()]}`;
}

export function elapsedText(seconds: number): string {
  const minutes = Math.floor(Math.abs(seconds) / 60);
  const hours = Math.floor(minutes / 60);
  return hours >= 24 ? `${Math.floor(hours / 24)}d ${hours % 24}h`
    : hours > 0 ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
}

export function clockText(epoch: number, offset: number): string {
  const local = new Date((epoch + offset * 60) * 1000);
  return `${String(local.getUTCHours()).padStart(2, '0')}:${String(local.getUTCMinutes()).padStart(2, '0')}`;
}

interface RulerMark { column: number; text: string; tone: Tone }

/** Later marks displace entire earlier labels, never pieces of them. */
function rulerLabels(marks: readonly RulerMark[], width: number): Cell[] {
  const placed: { start: number; mark: RulerMark }[] = [];
  for (const mark of marks) {
    const size = cellWidth(mark.text);
    if (size > width) continue;
    const start = Math.max(0, Math.min(width - size, mark.column));
    for (let index = placed.length - 1; index >= 0; index--) {
      const old = placed[index]!;
      if (start <= old.start + cellWidth(old.mark.text) && old.start <= start + size) placed.splice(index, 1);
    }
    placed.push({ start, mark });
  }
  const labels: Cell[] = [];
  let column = 0;
  for (const { start, mark } of placed.sort((a, b) => a.start - b.start)) {
    while (column < start) { labels.push({ ch: ' ', tone: 'default' }); column++; }
    for (const ch of mark.text) { labels.push({ ch, tone: mark.tone }); column += cellWidth(ch); }
  }
  return labels;
}

export function labelRuns(marks: readonly { column: number; text: string }[], width: number): string {
  return rulerLabels(marks.map(mark => ({ ...mark, tone: 'default' })), width).map(cell => cell.ch).join('').trimEnd();
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
    ch: index <= column(now) ? '━' : '─', tone: index <= column(now) ? 'suggestion' : 'inactive',
  }));
  const marks: RulerMark[] = [{ column: 0, text: phase?.start?.text ?? clockText(from, utcOffsetMinutes), tone: 'default' }];
  for (const [target] of unit.eta?.trail ?? []) {
    track[column(target.at)] = { ch: '○', tone: 'inactive' };
    marks.push({ column: column(target.at), text: target.text, tone: 'default' });
  }
  if (promise) {
    track[column(promise.at)] = { ch: '●', tone: promise.at < now ? 'error' : 'success' };
    marks.push({ column: column(promise.at), text: promise.text, tone: promise.at < now ? 'error' : 'success' });
  }
  track[column(now)] = { ch: '│', tone: 'default' };
  marks.push({ column: column(now), text: clockText(now, utcOffsetMinutes), tone: 'default' });
  const labelCells = rulerLabels(marks, width);
  return { track, labels: labelCells.map(cell => cell.ch).join('').trimEnd(), labelCells, from, to };
}

export function signedMinutes(minutes: number): string {
  const size = Math.round(Math.abs(minutes));
  return `${minutes < 0 ? '-' : '+'}${Math.floor(size / 60)}:${String(size % 60).padStart(2, '0')}`;
}

export function duration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}`;
}
