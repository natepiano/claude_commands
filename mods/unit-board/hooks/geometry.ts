export type Tone = 'success' | 'error' | 'warning' | 'suggestion' | 'inactive' | 'default';

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
