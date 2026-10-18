/** Stored status from accounts.py status --json --stored --with-login. */
export interface StoredAccount {
  label: string;
  tool: 'claude' | 'codex';
  roles: string[];
  weekly_left_percent: number | null;
  resets_at: string | null;
  credits: number | 'unlimited' | null;
  use_credits: boolean;
  read_at: string | null;
  login?: string | null;
}
export interface StoredAccountsStatus {
  accounts: StoredAccount[];
  problems: string[];
}
