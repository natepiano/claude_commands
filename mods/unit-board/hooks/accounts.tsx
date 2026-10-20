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

interface AccountRow {
  label: string;
  roles: string;
  dot: Tone;
  percent: BoardLine;
  details: BoardLine[];
}

function accountRow(account: AccountDisplay, zone: string, now: number): AccountRow {
  const tone = leftTone(account.weekly);
  const dot = account.weekly.kind === 'unavailable' ? 'inactive' : tone === 'default' ? 'success' : tone;
  const percent = [span(account.weekly.kind === 'available' ? `${Math.round(account.weekly.value)}% left` : 'no reading', tone)];
  const details: BoardLine[] = [];
  const fact = (name: string, value: string) => {
    const parts = [span(name, 'inactive'), span(` ${value}`)];
    details.push(parts);
    return parts;
  };
  const reset = dateText(account.resets, zone, now);
  if (reset) fact('resets', reset);
  if (account.credits.kind !== 'unavailable') {
    const credits = fact('credits', account.credits.kind === 'unlimited' ? 'unlimited' : String(account.credits.amount));
    if (account.onCredits) credits.push(span(' in use', 'warning'));
  }
  const read = dateText(account.read, zone, now);
  if (read) fact('read', read);
  return { label: account.label, roles: account.roles.join(', '), dot, percent, details };
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

function accountLines(accounts: AccountRow[], width: number): BoardLine[] {
  const labelWidth = accounts.reduce((width, row) => Math.max(width, cellWidth(row.label)), 0);
  const rolesWidth = accounts.reduce((width, row) => Math.max(width, cellWidth(row.roles)), 0);
  const percentWidth = accounts.reduce((width, row) => Math.max(width, lineWidth(row.percent)), 0);
  const headings = accounts.map(row => [span('● ', row.dot), span(row.label, 'default', 'bold'),
    span(' '.repeat(labelWidth - cellWidth(row.label) + 2)),
    span(row.roles + ' '.repeat(rolesWidth - cellWidth(row.roles)))]);
  const singleLines = accounts.map((row, index) => [...headings[index]!, span('  '), ...row.percent,
    span(' '.repeat(percentWidth - lineWidth(row.percent))),
    ...(row.details.length ? [span(' · ', 'inactive'), ...joinFacts(row.details)] : [])]);
  if (singleLines.every(line => lineWidth(line) <= width)) return singleLines;

  const pairedHeadings = accounts.map((row, index) => [...headings[index]!, span('  '), ...row.percent]);
  const aligned = pairedHeadings.every(line => lineWidth(line) <= width);
  return accounts.flatMap((row, index) => {
    if (aligned) return [pairedHeadings[index]!, ...wrapFacts(row.details, width)];
    const heading = [span('● ', row.dot), span(row.label, 'default', 'bold')];
    if (row.roles) heading.push(span(`  ${row.roles}`));
    return [...wrapLine(heading, width), ...wrapFacts([row.percent, ...row.details], width)];
  });
}

export function AccountsView(elements: PaneElements, columns: number, status: StoredAccountsStatus, zone: string, now: number) {
  const { Box, Text } = elements;
  const width = Math.max(1, Math.floor(columns));
  const accounts = status.accounts.map(accountDisplay)
    .sort((left, right) => left.label.localeCompare(right.label, undefined, { sensitivity: 'base' }))
    .map(account => accountRow(account, zone, now));
  const rows = accountLines(accounts, width);
  const drawLine = (parts: BoardLine) => <Box flexDirection="row" flexShrink={0}>
    {parts.filter(part => part.text).map(part => part.tone === 'default'
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
