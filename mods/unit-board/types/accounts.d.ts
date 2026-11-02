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
  role_counts: (RoleSessionCounts | null)[];
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
}
