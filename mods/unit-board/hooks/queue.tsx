import { span, wrapLine } from './board';
import type { BoardLine, PaneElements } from './board';
import { cellWidth } from './geometry';
import type { QueueSnapshot } from '../types/queue';

function tableLines(header: string[], rows: string[][], width: number): BoardLine[] {
  if (!rows.length) return [[span('empty')]];
  const sizes = header.map((heading, index) =>
    rows.reduce((size, row) => Math.max(size, cellWidth(row[index] ?? '')), cellWidth(heading)));
  if (sizes.reduce((sum, size) => sum + size, 0) + (sizes.length - 1) * 2 <= width) {
    const line = (cells: string[], bold = false) => header.map((_, index) => {
      const cell = cells[index] ?? '';
      const padding = ' '.repeat(sizes[index]! - cellWidth(cell));
      const numeric = ['#', 'Promised', 'Using', 'Delta', 'Waiting since', 'Needs', 'Free'].includes(header[index]!);
      const aligned = numeric ? padding + cell : cell + padding;
      return span(aligned + (index === header.length - 1 ? '' : '  '), 'default', bold ? 'bold' : 'fact');
    });
    return [line(header, true), ...rows.map(row => line(row))];
  }
  return rows.flatMap((row, index) => [
    ...(index ? [[span('')]] : []),
    ...header.flatMap((heading, cell) => {
      const label = `${heading}: `;
      const value = row[cell] ?? '';
      const numeric = ['#', 'Promised', 'Using', 'Delta', 'Waiting since', 'Needs', 'Free'].includes(heading);
      if (numeric && cellWidth(label) + cellWidth(value) <= width) {
        return [[span(label, 'default', 'bold'),
          span(' '.repeat(width - cellWidth(label) - cellWidth(value)) + value)]];
      }
      return wrapLine([span(label, 'default', 'bold'), span(value)], width, 2);
    }),
  ]);
}

export function queueLines(snapshot: QueueSnapshot, columns: number): BoardLine[] {
  const width = Math.max(1, Math.floor(columns));
  const lines = [
    ...wrapLine([span('Running', 'default', 'bold')], width),
    ...tableLines(snapshot.running_header, snapshot.running, width),
    [span('')],
  ];
  if (snapshot.head.text) lines.push(...wrapLine([span(snapshot.head.text, snapshot.head.tone)], width));
  lines.push(...wrapLine([span('Waiting', 'default', 'bold')], width),
    ...tableLines(snapshot.waiting_header, snapshot.waiting, width), [span('')]);
  const totals = snapshot.totals;
  lines.push(...wrapLine([span(`Totals: promised ${totals.promised}; measured ${totals.measured}; `
    + `free ${totals.free}; reserve ${totals.reserve}`)], width));
  for (const problem of snapshot.problems) {
    lines.push(...wrapLine([span(`Roster: ${problem}; Showrunner and Unit show — for unmatched steps`, 'warning')], width));
  }
  return lines;
}

export function QueueView(elements: PaneElements, columns: number, snapshot: QueueSnapshot) {
  const { Box, Text } = elements;
  return <Box flexDirection="column">
    {queueLines(snapshot, columns).map(line => <Box flexDirection="row" flexShrink={0}>
      {line.map(part => part.tone === 'default'
        ? <Text bold={part.kind === 'bold'}>{part.text}</Text>
        : <Text color={part.tone} bold={part.kind === 'bold'}>{part.text}</Text>)}
    </Box>)}
  </Box>;
}
