import type { PaneElements } from './board';
import type { StoredAccount, StoredAccountsStatus } from '../types/accounts';
import { cellWidth } from './geometry';

export type AccountReading<Value> = { kind: 'available'; value: Value } | { kind: 'unavailable' };
export type CreditBalance = { kind: 'finite'; amount: number } | { kind: 'unlimited' } | { kind: 'unavailable' };
export interface AccountDisplay {
  label: string;
  roles: string[];
  weekly: AccountReading<number>;
  resets: AccountReading<string>;
  credits: CreditBalance;
  onCredits: boolean;
  read: AccountReading<string>;
}

export interface AccountsTable {
  headings: string[];
  rows: string[][];
  widths: number[];
  width: number;
}

type LeftTone = 'error' | 'warning' | 'normal';

function leftTone(weekly: AccountReading<number>): LeftTone {
  if (weekly.kind === 'unavailable') return 'normal';
  return weekly.value < 10 ? 'error' : weekly.value < 25 ? 'warning' : 'normal';
}

function reading<Value>(value: Value | null | undefined): AccountReading<Value> {
  return value === null || value === undefined ? { kind: 'unavailable' } : { kind: 'available', value };
}

export function accountDisplay(account: StoredAccount): AccountDisplay {
  return {
    label: account.label, roles: account.roles,
    weekly: reading(account.weekly_left_percent), resets: reading(account.resets_at),
    credits: account.credits === null ? { kind: 'unavailable' }
      : account.credits === 'unlimited' ? { kind: 'unlimited' } : { kind: 'finite', amount: account.credits },
    onCredits: account.use_credits, read: reading(account.read_at),
  };
}

function dateText(value: AccountReading<string>, zone: string): string {
  if (value.kind === 'unavailable') return '—';
  const date = new Date(value.value);
  if (!Number.isFinite(date.getTime())) return '—';
  return new Intl.DateTimeFormat('en-US', {
    timeZone: zone, month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(date).replace(/,/g, '');
}

export function accountsTable(rows: AccountDisplay[], zone: string): AccountsTable {
  const headings = ['Label', 'Roles', 'Left', 'Resets', 'Credits', 'Read'];
  const cells = rows.map(row => [row.label, row.roles.join(', '),
    row.weekly.kind === 'available' ? `${Math.round(row.weekly.value)}%` : '—',
    dateText(row.resets, zone),
    row.credits.kind === 'unavailable' ? '—'
      : `${row.credits.kind === 'finite' ? row.credits.amount : 'unlimited'}${row.onCredits ? ' in use' : ''}`,
    dateText(row.read, zone)]);
  const widths = headings.map((heading, index) => Math.max(cellWidth(heading), ...cells.map(row => cellWidth(row[index]!))));
  const width = widths.reduce((total, column) => total + column, 0) + 2 * (headings.length - 1);
  return { headings, rows: cells, widths, width };
}

export function AccountsView(elements: PaneElements, status: StoredAccountsStatus, zone: string) {
  const { Box, Text } = elements;
  const rows = status.accounts.map(accountDisplay);
  const table = accountsTable(rows, zone);
  const line = (cells: string[]) => cells.map((cell, index) => index < cells.length - 1
    ? cell + ' '.repeat(table.widths[index]! - cellWidth(cell)) : cell).join('  ');
  const left = table.headings.indexOf('Left');
  return <Box flexDirection="column">
    <Text bold>{line(table.headings)}</Text>
    {table.rows.map((cells, index) => {
      const tone = leftTone(rows[index]!.weekly);
      return <Text wrap="wrap">{cells.map((cell, column) => [
        column === left && tone !== 'normal' ? <Text color={tone}>{cell}</Text> : <Text>{cell}</Text>,
        column < cells.length - 1 ? <Text>{' '.repeat(table.widths[column]! - cellWidth(cell))}{'  '}</Text> : null,
      ])}</Text>;
    })}
    {status.problems.length > 0 && <Box flexDirection="column" marginTop={1}>
      <Text bold>Problems</Text>
      {status.problems.map(problem => <Text color="warning" wrap="wrap">{problem}</Text>)}
    </Box>}
  </Box>;
}
