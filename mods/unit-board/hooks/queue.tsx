import type { PaneElements } from './board';
import type { QueueLines } from '../types/queue';

/** Layout belongs to Python. Only the recorded countdown changes between reads. */
export function QueueView(elements: PaneElements, columns: number, data: QueueLines, now = data.as_of) {
  const { Box, Text } = elements;
  return <Box flexDirection="column" width={columns} overflow="hidden">
    {data.lines.map(line => <Box flexDirection="row" flexShrink={0} height={1} overflow="hidden">
      {line.map(part => {
        const left = part.until === undefined ? undefined : Math.max(0, Math.floor(part.until - now));
        const text = left === undefined ? part.text : `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
        return <Text wrap="truncate" bold={part.bold ?? false}
          {...(!part.tone || part.tone === 'default' ? {} : { color: part.tone })}
          {...(!part.background || part.background === 'default' ? {} : { backgroundColor: part.background })}>{text}</Text>;
      })}
    </Box>)}
  </Box>;
}
