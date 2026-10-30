import type { PaneElements } from './board';
import type { QueueLines } from '../types/queue';

/** Layout belongs to Python. The pane paints spans and clips at its edge. */
export function QueueView(elements: PaneElements, columns: number, data: QueueLines) {
  const { Box, Text } = elements;
  return <Box flexDirection="column" width={columns} overflow="hidden">
    {data.lines.map(line => <Box flexDirection="row" flexShrink={0} height={1} overflow="hidden">
      {line.map(part => part.tone === 'default'
        ? <Text wrap="truncate" bold={part.bold}>{part.text}</Text>
        : <Text wrap="truncate" color={part.tone} bold={part.bold}>{part.text}</Text>)}
    </Box>)}
  </Box>;
}
