/** Files and complete snapshots from one generator invocation. */
export interface BoardSnapshot<Production> {
  files: string[];
  watch: string[];
  loadedAt: number;
  boards: Production[];
}

export type PaneState<Production> =
  | { kind: 'loading' }
  | { kind: 'board'; data: BoardSnapshot<Production>; now: number }
  | { kind: 'unit'; data: BoardSnapshot<Production>; production: string; unit: string; now: number }
  | { kind: 'failed'; reason: string };

export type AccountsPaneState<Status> =
  | { kind: 'loading' }
  | { kind: 'accounts'; data: Status; zone: string; now: number }
  | { kind: 'failed'; reason: string };

/** The engine contract keeps snapshots opaque; hooks bind Production to BoardData. */
/** Missing keys are closed panes. Timers belong to the module, never persisted. */
declare module 'claude-code' {
  interface PluginState {
    'unit-board': { panes: Record<string, PaneState<unknown>>; accountsPanes: Record<string, AccountsPaneState<unknown>>; queuePanes: Record<string, import('./queue').QueuePaneState> };
  }
}
