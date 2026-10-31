import type { ProcessRunResult } from 'claude-code';
import type { StoredAccountsStatus } from '../types/accounts';
import type { BoardData, BoardResult } from '../types/board';
import type { BoardSnapshot } from '../types';

export type RefreshPaths =
  | { kind: 'board'; board: string; sources: string[] }
  | { kind: 'discovery'; sources: string[]; loadedAt: number };
export interface RefreshFiles { stat(path: string): Promise<{ mtimeMs: number }> }
export interface BoardLoader {
  home(): Promise<string | undefined>;
  registry(): Promise<string | undefined>;
  now(): Promise<number>;
  run(argv: readonly string[]): Promise<ProcessRunResult>;
  read(path: string): Promise<string>;
}

/** Shared by panes; it owns neither a timer nor any pane's state. */
export async function refreshIfChanged<T>(
  files: RefreshFiles,
  paths: readonly RefreshPaths[],
  generator: () => Promise<T>,
  set: (fresh: T) => Promise<void>,
  isOpen: () => boolean,
): Promise<boolean> {
  let changed = false;
  for (const path of paths) {
    if (!isOpen()) return false;
    let written: number;
    if (path.kind === 'discovery') written = path.loadedAt;
    else {
      try { written = (await files.stat(path.board)).mtimeMs; }
      catch { changed = true; continue; }
    }
    for (const source of path.sources) {
      if (!isOpen()) return false;
      try { if ((await files.stat(source)).mtimeMs > written) changed = true; }
      catch { changed = true; }
    }
  }
  if (!changed || !isOpen()) return false;
  const fresh = await generator();
  if (!isOpen()) return false;
  await set(fresh);
  return true;
}

export function snapshotPaths(snapshot: BoardSnapshot<BoardData>): RefreshPaths[] {
  return [
    ...snapshot.files.map((board, index): RefreshPaths => ({ kind: 'board', board, sources: snapshot.boards[index]?.sources ?? [] })),
    { kind: 'discovery', sources: snapshot.watch, loadedAt: snapshot.loadedAt },
  ];
}

export async function loadBoard(
  calls: BoardLoader, session: string, isOpen: () => boolean,
  watch: (paths: RefreshPaths) => void,
): Promise<BoardSnapshot<BoardData>> {
  const ensureOpen = () => { if (!isOpen()) throw new Error('Pane closed.'); };
  ensureOpen();
  const home = await calls.home();
  if (!home) throw new Error('Home directory is unavailable.');
  const registry = await calls.registry();
  const loadedAt = await calls.now();
  ensureOpen();
  watch({ kind: 'discovery', sources: [registry || `${home}/.local/state/notifier`], loadedAt });
  const result = await calls.run([
    `${home}/.claude/scripts/lib/py`, `${home}/.claude/scripts/direct/board_json.py`, '--session', session,
  ]);
  ensureOpen();
  if (result.exitCode !== 0) throw new Error(result.stderr.trim() || 'The board could not be refreshed.');
  const response = JSON.parse(result.stdout) as BoardResult;
  for (const paths of [response.files, response.watch]) {
    if (!Array.isArray(paths) || !paths.every(path => typeof path === 'string')) {
      throw new Error('The board generator returned an invalid path list.');
    }
  }
  watch({ kind: 'discovery', sources: response.watch, loadedAt });
  const boards: BoardData[] = [];
  for (const path of response.files) {
    ensureOpen();
    boards.push(JSON.parse(await calls.read(path)) as BoardData);
  }
  ensureOpen();
  return { files: response.files, boards, watch: response.watch, loadedAt };
}

export interface AccountsLoader {
  home(): Promise<string | undefined>;
  now(): Promise<number>;
  run(argv: readonly string[]): Promise<ProcessRunResult>;
  stat(path: string): Promise<{ mtimeMs: number }>;
}

export interface UserZoneSettings {
  override(): Promise<string | undefined>;
  config(): Promise<string | undefined>;
  home(): Promise<string | undefined>;
  read(path: string): Promise<string>;
}
export type ZoneLookup = { kind: 'resolved'; name: string } | { kind: 'cancelled' };

/** Matches scripts/lib/user_zone.py, including its configurable settings path. */
export async function resolveUserZone(calls: UserZoneSettings, isActive: () => boolean): Promise<ZoneLookup> {
  if (!isActive()) return { kind: 'cancelled' };
  let name = await calls.override();
  if (!isActive()) return { kind: 'cancelled' };
  if (!name) {
    const configured = await calls.config();
    if (!isActive()) return { kind: 'cancelled' };
    const home = configured ? '' : await calls.home();
    if (!isActive()) return { kind: 'cancelled' };
    let contents = '';
    try { contents = await calls.read(configured || `${home}/.claude/config/user_zone.conf`); }
    catch { /* An absent setting uses the repository's default. */ }
    if (!isActive()) return { kind: 'cancelled' };
    name = contents.split(/\r?\n/).find(line => line.startsWith('zone='))?.split('=').slice(1).join('=').trim();
  }
  name ||= 'America/New_York';
  // Validate before storing the zone, as the Python resolver does.
  new Intl.DateTimeFormat('en-US', { timeZone: name });
  return { kind: 'resolved', name };
}

/** Only stored-status inputs and their existing discovery directories are inspected. */
export async function loadAccounts(
  calls: AccountsLoader, isOpen: () => boolean, watch: (paths: RefreshPaths[]) => void,
): Promise<StoredAccountsStatus> {
  const ensureOpen = () => { if (!isOpen()) throw new Error('Pane closed.'); };
  ensureOpen();
  const home = await calls.home();
  if (!home) throw new Error('Home directory is unavailable.');
  const loadedAt = await calls.now();
  const sources = new Set<string>();
  const inputs = [`${home}/.local/state/agent-notes/usage.json`, `${home}/.local/state/accounts/groups.json`,
    `${home}/.claude/config/accounts.json`, `${home}/.local/state/accounts/role_map_cache.json`];
  for (const input of inputs) {
    let path = input;
    while (path) {
      ensureOpen();
      try {
        await calls.stat(path);
        sources.add(path);
        break;
      } catch { /* A missing input is discovered through its closest existing parent. */ }
      path = path.slice(0, path.lastIndexOf('/')) || (path === '/' ? '' : '/');
    }
    watch([{ kind: 'discovery', sources: [...sources], loadedAt }]);
  }
  ensureOpen();
  const result = await calls.run([`${home}/.claude/scripts/lib/py`, `${home}/.claude/scripts/accounts/accounts.py`,
    'status', '--json', '--stored']);
  ensureOpen();
  // Failure details never leave the pane loader: the pane shows a fixed reason, never process output.
  if (result.exitCode !== 0) throw new Error('Stored accounts status could not be read.');
  try {
    const status = JSON.parse(result.stdout) as StoredAccountsStatus;
    if (!Array.isArray(status.accounts) || !Array.isArray(status.problems)) throw new Error();
    return status;
  } catch { throw new Error('Stored accounts status is invalid.'); }
}
