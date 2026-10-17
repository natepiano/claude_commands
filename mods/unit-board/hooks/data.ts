import type { ProcessRunResult } from 'claude-code';
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
  calls: BoardLoader, cwd: string, isOpen: () => boolean,
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
    `${home}/.claude/scripts/lib/py`, `${home}/.claude/scripts/delegate/board_json.py`, '--cwd', cwd,
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
  return { files: response.files, boards, focus: response.focus, watch: response.watch, loadedAt };
}
