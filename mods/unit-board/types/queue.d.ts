/** Already laid out in terminal cells by the queue reader. */
export type Tone = 'default' | 'success' | 'error' | 'warning' | 'inactive' | 'suggestion';
export interface QueueLines {
  columns: number;
  as_of: number;
  lines: { text: string; tone?: Tone; bold?: boolean; background?: Tone; until?: number }[][];
}

export type QueueSelection = { kind: 'view' } | { kind: 'card'; selector: string };

export type QueuePaneState =
  | { kind: 'loading'; selection: QueueSelection }
  | { kind: 'queue'; data: QueueLines; selection: QueueSelection }
  | { kind: 'failed'; reason: string; selection: QueueSelection };
