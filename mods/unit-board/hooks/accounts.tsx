import type { PaneElements } from './board';
import type { StoredAccount, StoredAccountsStatus } from '../types/accounts';

export type AccountReading<Value> = { kind: 'available'; value: Value } | { kind: 'unavailable' };
export type CreditBalance = { kind: 'finite'; amount: number } | { kind: 'unlimited' } | { kind: 'unavailable' };
export interface AccountDisplay {
  label: string;
  login: AccountReading<string>;
  roles: string[];
  weekly: AccountReading<number>;
  resets: AccountReading<string>;
  credits: CreditBalance;
  onCredits: boolean;
  read: AccountReading<string>;
}

function reading<Value>(value: Value | null | undefined): AccountReading<Value> {
  return value === null || value === undefined ? { kind: 'unavailable' } : { kind: 'available', value };
}

export function accountDisplay(account: StoredAccount): AccountDisplay {
  return {
    label: account.label, login: reading(account.login), roles: account.roles,
    weekly: reading(account.weekly_left_percent), resets: reading(account.resets_at),
    credits: account.credits === null ? { kind: 'unavailable' }
      : account.credits === 'unlimited' ? { kind: 'unlimited' } : { kind: 'finite', amount: account.credits },
    onCredits: account.use_credits, read: reading(account.read_at),
  };
}

function dateText(value: AccountReading<string>, zone: string): string {
  if (value.kind === 'unavailable') return 'unavailable';
  const date = new Date(value.value);
  if (!Number.isFinite(date.getTime())) return 'unavailable';
  return new Intl.DateTimeFormat('en-US', {
    timeZone: zone, weekday: 'short', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(date);
}

export function AccountsView(elements: PaneElements, status: StoredAccountsStatus, zone: string) {
  const { Box, Text } = elements;
  const rows = status.accounts.map(accountDisplay);
  const assignments = (...roles: string[]) => rows.filter(row => roles.some(role => row.roles.includes(role))).map(row => row.label).join(', ') || 'unassigned';
  const headings = ['Label', 'Login', 'Roles', 'Weekly left', 'Weekly resets', 'On credits', 'Credits left', 'Read'];
  const contents = rows.map(row => [row.label,
    row.login.kind === 'available' ? row.login.value : 'unavailable', row.roles.join(', '),
    row.weekly.kind === 'available' ? `${Math.round(row.weekly.value)}%` : 'unavailable',
    dateText(row.resets, zone), row.onCredits ? 'yes' : 'no',
    row.credits.kind === 'finite' ? String(row.credits.amount) : row.credits.kind,
    dateText(row.read, zone)]);
  const widths = headings.map((heading, index) => Math.max(heading.length, ...contents.map(row => row[index]!.length)));
  const line = (cells: string[]) => cells.map((cell, index) => cell.padEnd(widths[index]!)).join('  ');
  return <Box flexDirection="column">
    <Text bold>units: {assignments('units', 'unit directors')}, showrunners: {assignments('showrunners')}</Text>
    <Box flexDirection="column" marginTop={1}>
      <Text bold>{line(headings)}</Text>
      {contents.map((cells, index) => {
        const weekly = rows[index]!.weekly;
        const tone = weekly.kind === 'available' && weekly.value < 10 ? 'error'
          : weekly.kind === 'available' && weekly.value < 25 ? 'warning' : 'subtle';
        return <Text wrap="wrap">{cells.map((cell, column) => [
          <Text color={column === 3 ? tone : 'subtle'}>{cell}</Text>,
          <Text>{' '.repeat(widths[column]! - cell.length)}{column < cells.length - 1 ? '  ' : ''}</Text>,
        ])}</Text>;
      })}
    </Box>
    {status.problems.length > 0 && <Box flexDirection="column" marginTop={1}>
      <Text bold>Problems</Text>
      {status.problems.map(problem => <Text color="warning" wrap="wrap">{problem}</Text>)}
    </Box>}
  </Box>;
}
