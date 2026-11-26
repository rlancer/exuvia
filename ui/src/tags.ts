import type { TagFile } from "./api";

/*
 * Code tags, as the workbench checks and words them. The server has the last
 * say (exuvia/workspace.py TAG_NAME); this only explains a name before it is sent.
 */

const TAG_NAME = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9_])?$/;

/** Why `name` cannot be a tag, in words; null when it can. */
export function tagNameProblem(name: string): string | null {
  const n = name.trim();
  if (!n) return "Give it a name.";
  if (n.length > 64) return "Up to 64 characters.";
  if (!TAG_NAME.test(n)) return "Letters, digits, '.', '_' and '-' only, starting with a letter or digit and not ending in '.' or '-'.";
  if (n.includes("..") || n.endsWith(".lock")) return "No '..' and no '.lock' at the end (git could not take it).";
  return null;
}

/** A file's state since the tag, in words. */
export const tagStatusLabel: Record<TagFile["status"], string> = {
  same: "same",
  changed: "changed",
  deleted: "deleted",
  added: "new",
};
