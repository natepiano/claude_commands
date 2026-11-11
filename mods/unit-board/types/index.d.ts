export type AccountsPaneState<Status> =
  | { kind: 'loading' }
  | { kind: 'accounts'; data: Status; zone: string; now: number }
  | { kind: 'failed'; reason: string };

/** Missing keys are closed panes. Timers belong to the module, never persisted. */
declare module 'claude-code' {
  interface PluginState {
    'unit-board': { accountsPanes: Record<string, AccountsPaneState<unknown>>; queuePanes: Record<string, import('./queue').QueuePaneState> };
  }
}
