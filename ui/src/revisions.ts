import type { CodeRevision } from "./api";

/*
 * Reading a list of code revisions (newest first, as GET /api/code/revisions
 * answers): which version a file has now, what came before one, which files
 * are deleted and how a version is made current again.
 */

/** The version of `path` that is its text now, if one in the list is. */
export function currentVersion(revs: CodeRevision[], path: string): CodeRevision | null {
  return revs.find((r) => r.path === path && r.current) ?? null;
}

/** The revision of the same file saved just before `rev`, if the list goes back that far. */
export function previousVersion(revs: CodeRevision[], rev: CodeRevision): CodeRevision | null {
  const i = revs.findIndex((r) => r.id === rev.id);
  if (i < 0) return null;
  return revs.slice(i + 1).find((r) => r.path === rev.path) ?? null;
}

/** Whether the newest revision of `path` in the list deleted it. */
export function isDeleted(revs: CodeRevision[], path: string): boolean {
  const newest = revs.find((r) => r.path === path);
  return !!newest && newest.hash === null;
}

/**
 * Files whose newest revision deleted them, newest deletion first, each with
 * the last version saved before it (null when the list does not reach it).
 */
export function deletedFiles(revs: CodeRevision[]): { deleted: CodeRevision; last: CodeRevision | null }[] {
  const seen = new Set<string>();
  const out: { deleted: CodeRevision; last: CodeRevision | null }[] = [];
  for (const r of revs) {
    if (seen.has(r.path)) continue;
    seen.add(r.path);
    if (r.hash === null) out.push({ deleted: r, last: revs.find((x) => x.path === r.path && x.hash !== null) ?? null });
  }
  return out;
}

/** The distinct paths in the list, in the order they first appear (newest change first). */
export function paths(revs: CodeRevision[]): string[] {
  return [...new Set(revs.map((r) => r.path))];
}

/** How a version can be made current again: a node's, a view file's, or not from the workbench. */
export type RestoreTarget = { kind: "node"; name: string } | { kind: "view"; name: string; path: string };

export function restoreTarget(rev: CodeRevision): RestoreTarget | null {
  if (rev.node) return { kind: "node", name: rev.node };
  const prefix = `views/${rev.view}/`;
  if (rev.view && rev.path.startsWith(prefix)) return { kind: "view", name: rev.view, path: rev.path.slice(prefix.length) };
  return null;
}

/** "ui" → "workbench" etc.: what saved a revision, in words. */
export const ORIGIN_LABEL: Record<string, string> = {
  ui: "workbench",
  agent: "agent",
  api: "API",
  file: "file on disk",
  template: "template",
  restore: "restore",
  load: "load",
  import: "import",
};
