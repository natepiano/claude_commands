/** Already laid out in terminal cells by the queue reader. */
export type Tone = 'default' | 'success' | 'error' | 'warning' | 'inactive' | 'suggestion';
export interface QueueLines {
  columns: number;
  as_of: number;
  lines: { text: string; tone?: Tone; bold?: boolean; background?: Tone; until?: number }[][];
}

export type QueuePaneState =
  | { kind: 'loading'; selector: string | null }
  | { kind: 'queue'; data: QueueLines; selector: string | null }
  | { kind: 'failed'; reason: string; selector: string | null };
