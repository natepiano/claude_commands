/** The disk contract written by scripts/direct/board_json.py. */
export interface BoardTime { at: number; text: string }
export type EtaTrail = [eta_at: BoardTime, stated_at: BoardTime][];
export type BoardGate =
  | { kind: 'none' }
  | { kind: 'timed'; gate: string; until: BoardTime; waits_on: string }
  | { kind: 'untimed'; gate: string; waits_on: string };
export type BoardFinish =
  | { kind: 'predicted' | 'finished'; at: BoardTime }
  | { kind: 'after_gate'; gate: string; waits_on: string }
  | { kind: 'unknown' };
export interface BoardFindings { blocker: number; minor: number; nit: number }
export interface BoardPhase {
  id: string; title: string; status: 'done' | 'open' | 'todo' | 'gated';
  took: number | null; fix_done: number; fix_running: number;
  findings: BoardFindings;
  eta: { first: BoardTime | null; trail: EtaTrail; finished: BoardTime | null; delta_minutes: number | null };
  start: BoardTime | null; finish: BoardTime | null; gate: BoardGate;
}
export interface BoardEta {
  at: BoardTime | null; first: BoardTime | null; trail: EtaTrail;
  moved: number; delta_minutes: number | null; schedule_end: BoardTime | null;
  fix_done: number; fix_running: number;
}
export type BoardStep = [state: string, what: string, started: BoardTime, took_seconds: number];
export interface BoardWorker { slot: string; role: string; state: string; model: string; effort?: string; doing: string }
export interface PlanPhase { kind: 'plan'; id: string; of: number; title: string }
export interface TicketPhase { kind: 'ticket'; id: string; title: string; merged: number }
export interface BoardUnit {
  key: string; plan: string; state: 'running' | 'run-finished' | 'standing-by';
  phase: PlanPhase | TicketPhase | null;
  phases: BoardPhase[]; finish: BoardFinish; stage: string; eta: BoardEta | null;
  summary: string; steps: BoardStep[]; workers: BoardWorker[];
}
export interface BoardData {
  production: string; as_of: BoardTime; zone: string; utc_offset_minutes: number;
  units: BoardUnit[]; sources: string[];
}
export interface BoardResult { files: string[]; watch: string[] }
