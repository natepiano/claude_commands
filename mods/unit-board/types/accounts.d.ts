/** Stored status from accounts.py status --json --stored. */
export interface StoredAccount {
  label: string;
  tool: 'claude' | 'codex';
  roles: string[];
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
}
