/** Cells are formatted once by build_queue.py, including times in the user's zone. */
export interface QueueSnapshot {
  running_header: string[];
  running: string[][];
  waiting_header: string[];
  waiting: string[][];
  head: { text: string; tone: 'default' | 'error' };
  totals: { promised: string; measured: string; free: string; reserve: string };
  problems: string[];
}

export type QueuePaneState =
  | { kind: 'loading' }
  | { kind: 'queue'; data: QueueSnapshot }
  | { kind: 'failed'; reason: string };

