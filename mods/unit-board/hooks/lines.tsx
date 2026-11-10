import type { Elements } from 'claude-code';
import type { Tone } from './geometry';
import { cellWidth, wrappedLines } from './geometry';

export type PaneElements = Pick<Elements['terminal'], 'Box' | 'Text' | 'Button'>;

export interface BoardSpan { text: string; tone: Tone; kind: 'fact' | 'name' | 'bold' }
export type BoardLine = BoardSpan[];
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
