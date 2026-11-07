import { span, wrapLine } from './board';
import type { BoardLine, PaneElements } from './board';
import type { RoleSessionCounts, StoredAccount, StoredAccountsStatus } from '../types/accounts';
import { cellWidth } from './geometry';
import type { Tone } from './geometry';

export type AccountReading<Value> = { kind: 'available'; value: Value } | { kind: 'unavailable' };
export type CreditBalance = { kind: 'finite'; amount: number } | { kind: 'unlimited' } | { kind: 'unavailable' };
export interface AccountDisplay {
  label: string;
  roles: string[];
  roleCounts: AccountReading<RoleSessionCounts>[];
  weekly: AccountReading<number>;
  resets: AccountReading<string>;
  credits: CreditBalance;
  useCredits: boolean;
  readOk: boolean;
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
    label: account.label, roles: account.roles.map(role =>
      account.selected_roles?.includes(role) ? `${role} ✓` : role),
    roleCounts: account.roles.map((_, index) => reading(account.role_counts?.[index])),
    weekly: reading(account.weekly_left_percent), resets: reading(account.resets_at),
    credits: account.credits === null ? { kind: 'unavailable' }
      : account.credits === 'unlimited' ? { kind: 'unlimited' } : { kind: 'finite', amount: account.credits },
    useCredits: account.use_credits, readOk: account.read_ok === true,
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

type AccountColumn = 'dot' | 'label' | 'roles' | 'busy' | 'shell' | 'idle' | 'total' | 'left' | 'resets' | 'credits' | 'enabled';
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

const COUNT_COLUMNS = ['busy', 'shell', 'idle', 'total'] as const;
type CountColumn = typeof COUNT_COLUMNS[number];

function countCells(count: AccountReading<RoleSessionCounts>): Record<CountColumn, string> {
  return Object.fromEntries(COUNT_COLUMNS.map(key => {
    const value = count.kind === 'available' ? count.value[key] : null;
    return [key, value === null ? '' : String(value)];
  })) as Record<CountColumn, string>;
}

function countSpans(counts: Record<CountColumn, string>): Record<CountColumn, BoardLine> {
  return { busy: [span(counts.busy)], shell: [span(counts.shell)],
    idle: [span(counts.idle)], total: [span(counts.total)] };
}

function totalRow(accounts: AccountDisplay[]): AccountRow {
  const counts = accounts.flatMap(account => account.roleCounts.flatMap(count =>
    count.kind === 'available' ? [count.value] : []));
  const knownShell = counts.flatMap(count => count.shell === null ? [] : [count.shell]);
  const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);
  const cells = countCells(reading<RoleSessionCounts>({
    busy: sum(counts.map(count => count.busy)),
    shell: knownShell.length ? sum(knownShell) : null,
    idle: sum(counts.map(count => count.idle)), total: sum(counts.map(count => count.total)),
  }));
  return { cells: {
    dot: [], label: [span('total', 'default', 'bold')], left: [], roles: [], ...countSpans(cells),
    resets: [], credits: [], enabled: [],
  }, facts: COUNT_COLUMNS.flatMap(key => cells[key]
    ? [[span(key, 'inactive'), span(` ${cells[key]}`)]] : []) };
}

function accountRows(account: AccountDisplay, zone: string, now: number): AccountRow[] {
  const tone = leftTone(account.weekly);
  const dot = account.readOk ? 'success' : 'error';
  const left = account.weekly.kind === 'available' ? `${Math.round(account.weekly.value)}%` : '—';
  const reset = dateText(account.resets, zone, now);
  const balance = account.credits.kind === 'unavailable' ? '' : account.credits.kind === 'unlimited'
    ? 'unlimited' : new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(account.credits.amount);
  const credits = [span(balance)];
  return (account.roles.length ? account.roles : ['']).map((role, index) => {
    const first = index === 0;
    const counts = countCells(account.roleCounts[index] ?? { kind: 'unavailable' });
    const facts = first ? [[span(`${left} left`, tone)]] : [];
    if (role) facts.push([span('role', 'inactive'), span(` ${role}`)]);
    for (const key of COUNT_COLUMNS) {
      if (counts[key]) facts.push([span(key, 'inactive'), span(` ${counts[key]}`)]);
    }
    if (first && reset) facts.push([span('resets', 'inactive'), span(` ${reset}`)]);
    if (first && balance) facts.push([span('credits', 'inactive'), span(' '), ...credits]);
    if (first && account.useCredits) facts.push([span('enabled')]);
    return { cells: {
      dot: first ? [span('●', dot)] : [], label: first ? [span(account.label, 'default', 'bold')] : [],
      roles: [span(role)], ...countSpans(counts), left: first ? [span(left, tone)] : [],
      resets: first ? [span(reset)] : [], credits: first ? credits : [],
      enabled: first ? [span(account.useCredits ? 'yes' : '')] : [],
    }, facts };
  });
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

function tableColumns(accounts: AccountRow[]): TableColumn[] {
  const columns: TableColumn[] = [
    { key: 'dot', heading: '', align: 'left', width: 0 },
    { key: 'label', heading: 'account', align: 'left', width: 0 },
    { key: 'left', heading: 'left', align: 'right', width: 0 },
    { key: 'roles', heading: 'role', align: 'left', width: 0 },
    ...COUNT_COLUMNS.map(key => ({ key, heading: key, align: 'right' as const, width: 0 })),
    { key: 'resets', heading: 'resets', align: 'right', width: 0 },
    { key: 'credits', heading: 'credits', align: 'right', width: 0 },
    { key: 'enabled', heading: 'enabled', align: 'left', width: 0 },
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

function accountLines(accounts: AccountRow[], width: number): BoardLine[] {
  if (!accounts.length) return [];
  const columns = tableColumns(accounts);
  const header = Object.fromEntries(columns.map(column =>
    [column.key, [span(column.heading, 'default', 'bold')]])) as Record<AccountColumn, BoardLine>;
  const tableWidth = (columns: TableColumn[], indent = 0) =>
    indent + columns.reduce((width, column) => width + column.width, 0) + (columns.length - 1) * 2;
  if (tableWidth(columns) <= width) {
    return [tableLine(columns, header), ...accounts.map(row => tableLine(columns, row.cells))];
  }
  const first = columns.slice(0, 8);
  const second = columns.slice(8);
  if (tableWidth(first) <= width && tableWidth(second, 2) <= width) {
    return [tableLine(first, header), tableLine(second, header, 2),
      ...accounts.flatMap(row => [tableLine(first, row.cells),
        ...(row.cells.label.length ? [tableLine(second, row.cells, 2)] : [])])];
  }
  return accounts.flatMap(row => {
    const heading = [...row.cells.dot, ...(row.cells.dot.length ? [span(' ')] : []), ...row.cells.label];
    return [...(heading.length ? wrapLine(heading, width) : []), ...wrapFacts(row.facts, width)];
  });
}

export function AccountsView(elements: PaneElements, columns: number, status: StoredAccountsStatus, zone: string, now: number) {
  const { Box, Text } = elements;
  const width = Math.max(1, Math.floor(columns));
  const accounts = status.accounts.map(accountDisplay)
    .sort((left, right) => left.label.localeCompare(right.label, undefined, { sensitivity: 'base' }));
  const rows = accountLines(accounts.length
    ? [...accounts.flatMap(account => accountRows(account, zone, now)), totalRow(accounts)] : [], width);
  const drawLine = (parts: BoardLine) => <Box flexDirection="row" flexShrink={0}>
    {(parts.some(part => part.text) ? parts.filter(part => part.text) : [span('')]).map(part => part.tone === 'default'
      ? <Text bold={part.kind === 'bold'}>{part.text}</Text>
      : <Text color={part.tone} bold={part.kind === 'bold'}>{part.text}</Text>)}
  </Box>;
  return <Box flexDirection="column">
    {rows.map(drawLine)}
    {status.upcoming_moves && <Box flexDirection="column" marginTop={1}>
      {status.upcoming_moves.flatMap(line => line ? wrapLine([span(line)], width) : [[]]).map(drawLine)}
    </Box>}
    {status.problems.length > 0 && <Box flexDirection="column" marginTop={1}>
      {wrapLine([span('Problems', 'default', 'bold')], width).map(drawLine)}
      {status.problems.flatMap(problem => wrapLine([span(`• ${problem}`, 'warning')], width)).map(drawLine)}
    </Box>}
  </Box>;
}
