/** Complete activity counts for one role. */
export interface RoleSessionCounts {
  busy: number;
  idle: number;
  paused: number;
  exited: number;
  total: number;
}
/** Older status producers also wrote starting and waiting counts, which are busy. */
export interface StoredRoleSessionCounts extends RoleSessionCounts {
  starting?: number;
  waiting?: number;
}
export interface StoredAccount {
  label: string;
  tool: 'claude' | 'codex';
  roles: string[];
  selected_roles?: string[];
  /** Combined counts in roles order; spare has no count. Only units and showrunners count exited members. */
  role_counts: (StoredRoleSessionCounts | null)[];
  /** Local and fresh peer counts, keyed by machine and role. */
  role_counts_by_machine?: Record<string, Record<string, StoredRoleSessionCounts>>;
  weekly_left_percent: number | null;
  resets_at: string | null;
  credits: number | 'unlimited' | null;
  use_credits: boolean;
  read_at: string | null;
  read_ok?: boolean;
  credits_in_use?: boolean;
}
export interface StoredAccountsStatus {
  accounts: StoredAccount[];
  problems: string[];
  session_inputs?: string[];
  upcoming_moves?: string[];
}
