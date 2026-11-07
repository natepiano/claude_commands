/** Stored status from accounts.py status --json --stored. */
export interface RoleSessionCounts {
  busy: number;
  shell: number | null;
  idle: number;
  total: number;
}
export interface StoredAccount {
  label: string;
  tool: 'claude' | 'codex';
  roles: string[];
  selected_roles?: string[];
  /** Combined counts in roles order; spare has no count. Codex shell counts are null. */
  role_counts: (RoleSessionCounts | null)[];
  /** Local and fresh peer counts, keyed by machine and role. */
  role_counts_by_machine?: Record<string, Record<string, RoleSessionCounts>>;
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
