import { span, wrapLine } from './board';
import type { BoardLine, PaneElements } from './board';
import type { StoredAccount, StoredAccountsStatus } from '../types/accounts';
import { cellWidth } from './geometry';
import type { Tone } from './geometry';

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

function leftTone(weekly: AccountReading<number>): Tone {
  if (weekly.kind === 'unavailable') return 'default';
  return weekly.value < 10 ? 'error' : weekly.value < 25 ? 'warning' : 'default';
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

/** Compare calendar days in the resolved zone, including across DST changes. */
function localDay(date: Date, zone: string): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: zone, year: 'numeric', month: 'numeric', day: 'numeric',
  }).formatToParts(date);
  const part = (type: string) => Number(parts.find(part => part.type === type)!.value);
  return Date.UTC(part('year'), part('month') - 1, part('day')) / 86400000;
}

function dateText(value: AccountReading<string>, zone: string, now: number): string {
  if (value.kind === 'unavailable') return '';
  const date = new Date(value.value);
  if (!Number.isFinite(date.getTime())) return '';
  const days = Math.abs(localDay(date, zone) - localDay(new Date(now * 1000), zone));
  if (days > 6) return new Intl.DateTimeFormat('en-GB', {
    timeZone: zone, day: 'numeric', month: 'short',
  }).format(date);
  const clock = new Intl.DateTimeFormat('en-GB', {
    timeZone: zone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(date);
  return days === 0 ? clock : `${new Intl.DateTimeFormat('en-US', { timeZone: zone, weekday: 'short' }).format(date)} ${clock}`;
}

type AccountColumn = 'dot' | 'label' | 'roles' | 'left' | 'resets' | 'credits' | 'read';
interface AccountRow {
  cells: Record<AccountColumn, BoardLine>;
  facts: BoardLine[];
}
interface TableColumn {
  key: AccountColumn;
  heading: string;
  align: 'left' | 'right';
  width: number;
}

function accountRow(account: AccountDisplay, zone: string, now: number): AccountRow {
  const tone = leftTone(account.weekly);
  const dot = account.weekly.kind === 'unavailable' ? 'inactive' : tone === 'default' ? 'success' : tone;
  const left = account.weekly.kind === 'available' ? `${Math.round(account.weekly.value)}%` : '—';
  const reset = dateText(account.resets, zone, now);
  const read = dateText(account.read, zone, now);
  const balance = account.credits.kind === 'unavailable' ? '' : account.credits.kind === 'unlimited'
    ? 'unlimited' : new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(account.credits.amount);
  const credits = [span(balance)];
  if (balance && account.onCredits) credits.push(span(' in use', 'warning'));
  const facts = [[span(account.weekly.kind === 'available' ? `${left} left` : 'no reading', tone)]];
  if (reset) facts.push([span('resets', 'inactive'), span(` ${reset}`)]);
  if (balance) facts.push([span('credits', 'inactive'), span(' '), ...credits]);
  if (read) facts.push([span('read', 'inactive'), span(` ${read}`)]);
  return { cells: {
    dot: [span('●', dot)], label: [span(account.label, 'default', 'bold')],
    roles: [span(account.roles.join(', '))], left: [span(left, tone)],
    resets: [span(reset)], credits, read: [span(read)],
  }, facts };
}

const lineWidth = (line: BoardLine) => line.reduce((width, part) => width + cellWidth(part.text), 0);
const joinFacts = (facts: BoardLine[]): BoardLine => facts.flatMap((fact, index) =>
  index ? [span(' · ', 'inactive'), ...fact] : fact);

/** Keep facts intact, dropping only the separator where a new row starts. */
function wrapFacts(facts: BoardLine[], width: number): BoardLine[] {
  const indent = Math.max(0, Math.min(2, width - 1));
  const room = width - indent;
  const lines: BoardLine[] = [];
  let current: BoardLine[] = [];
  let used = 0;
  const flush = () => {
    if (current.length) lines.push([span(' '.repeat(indent)), ...joinFacts(current)]);
    current = [];
    used = 0;
  };
  for (const fact of facts) {
    const size = lineWidth(fact);
    if (size > room) {
      flush();
      lines.push(...wrapLine(fact, width, indent));
      continue;
    }
    const separator = current.length ? cellWidth(' · ') : 0;
    if (used + separator + size > room) flush();
    used += (current.length ? cellWidth(' · ') : 0) + size;
    current.push(fact);
  }
  flush();
  return lines;
}

function tableColumns(accounts: AccountRow[], showCredits: boolean): TableColumn[] {
  const columns: TableColumn[] = [
    { key: 'dot', heading: '', align: 'left', width: 0 },
    { key: 'label', heading: 'Label', align: 'left', width: 0 },
    { key: 'roles', heading: 'Roles', align: 'left', width: 0 },
    { key: 'left', heading: 'Left', align: 'right', width: 0 },
    { key: 'resets', heading: 'Resets', align: 'right', width: 0 },
    ...(showCredits ? [{ key: 'credits', heading: 'Credits', align: 'left', width: 0 } as const] : []),
    { key: 'read', heading: 'Read', align: 'right', width: 0 },
  ];
  return columns.map(column => ({ ...column, width: accounts.reduce(
    (width, row) => Math.max(width, lineWidth(row.cells[column.key])), cellWidth(column.heading)) }));
}

function tableLine(columns: TableColumn[], cells: Record<AccountColumn, BoardLine>, indent = 0): BoardLine {
  const line = [span(' '.repeat(indent)), ...columns.flatMap((column, index) => {
    const cell = cells[column.key];
    const padding = span(' '.repeat(column.width - lineWidth(cell)));
    return [...(index ? [span('  ')] : []),
      ...(column.align === 'right' ? [padding, ...cell]
        : index === columns.length - 1 ? cell : [...cell, padding])];
  })];
  // Missing final cells leave no padding at the row's edge.
  while (line.length) {
    const last = line[line.length - 1]!;
    const text = last.text.trimEnd();
    if (text) {
      line[line.length - 1] = { ...last, text };
      break;
    }
    line.pop();
  }
  return line;
}

function accountLines(accounts: AccountRow[], width: number, showCredits: boolean): BoardLine[] {
  if (!accounts.length) return [];
  const columns = tableColumns(accounts, showCredits);
  const header = Object.fromEntries(columns.map(column =>
    [column.key, [span(column.heading, 'default', 'bold')]])) as Record<AccountColumn, BoardLine>;
  const tableWidth = (columns: TableColumn[], indent = 0) =>
    indent + columns.reduce((width, column) => width + column.width, 0) + (columns.length - 1) * 2;
  if (tableWidth(columns) <= width) {
    return [tableLine(columns, header), ...accounts.map(row => tableLine(columns, row.cells))];
  }
  const first = columns.slice(0, 4);
  const second = columns.slice(4);
  if (tableWidth(first) <= width && tableWidth(second, 2) <= width) {
    return [tableLine(first, header), tableLine(second, header, 2),
      ...accounts.flatMap(row => [tableLine(first, row.cells), tableLine(second, row.cells, 2)])];
  }
  return accounts.flatMap(row => {
    const heading = [...row.cells.dot, span(' '), ...row.cells.label];
    if (lineWidth(row.cells.roles)) heading.push(span('  '), ...row.cells.roles);
    return [...wrapLine(heading, width), ...wrapFacts(row.facts, width)];
  });
}

export function AccountsView(elements: PaneElements, columns: number, status: StoredAccountsStatus, zone: string, now: number) {
  const { Box, Text } = elements;
  const width = Math.max(1, Math.floor(columns));
  const accounts = status.accounts.map(accountDisplay)
    .sort((left, right) => left.label.localeCompare(right.label, undefined, { sensitivity: 'base' }))
    .map(account => accountRow(account, zone, now));
  const rows = accountLines(accounts, width, status.accounts.some(account => account.credits !== null));
  const drawLine = (parts: BoardLine) => <Box flexDirection="row" flexShrink={0}>
    {(parts.some(part => part.text) ? parts.filter(part => part.text) : [span('')]).map(part => part.tone === 'default'
      ? <Text bold={part.kind === 'bold'}>{part.text}</Text>
      : <Text color={part.tone} bold={part.kind === 'bold'}>{part.text}</Text>)}
  </Box>;
  return <Box flexDirection="column">
    {rows.map(drawLine)}
    {status.problems.length > 0 && <Box flexDirection="column" marginTop={1}>
      {wrapLine([span('Problems', 'default', 'bold')], width).map(drawLine)}
      {status.problems.flatMap(problem => wrapLine([span(`• ${problem}`, 'warning')], width)).map(drawLine)}
    </Box>}
  </Box>;
}
