import { describe, expect, it } from "vitest";
import type { CodeRevision } from "./api";
import { currentVersion, deletedFiles, isDeleted, paths, previousVersion, restoreTarget } from "./revisions";

let id = 0;
// Ids as the server sends them: 32 hex characters.
const rev = (path: string, hash: string | null, extra: Partial<CodeRevision> = {}): CodeRevision => ({
  id: (++id).toString(16).padStart(32, "0"),
  path,
  node: null,
  view: null,
  hash,
  origin: "ui",
  author: null,
  saved_at: 1000 - id,
  current: false,
  ...extra,
});

// Newest first, as the API answers.
const a3 = rev("nodes/a.py", null, { node: "a" });
const b2 = rev("nodes/b.py", "b2", { node: "b", current: true });
const a2 = rev("nodes/a.py", "a2", { node: "a" });
const v1 = rev("views/board/App.tsx", "v1", { view: "board", current: true });
const b1 = rev("nodes/b.py", "b1", { node: "b" });
const a1 = rev("nodes/a.py", "a1", { node: "a" });
const revs = [a3, b2, a2, v1, b1, a1];

describe("reading code revisions", () => {
  it("finds a file's current and previous versions", () => {
    expect(currentVersion(revs, "nodes/b.py")).toBe(b2);
    expect(currentVersion(revs, "nodes/a.py")).toBeNull();
    expect(previousVersion(revs, b2)).toBe(b1);
    expect(previousVersion(revs, a3)).toBe(a2);
    expect(previousVersion(revs, a1)).toBeNull();
  });

  it("lists deleted files with the last version saved before the deletion", () => {
    expect(isDeleted(revs, "nodes/a.py")).toBe(true);
    expect(isDeleted(revs, "nodes/b.py")).toBe(false);
    expect(deletedFiles(revs)).toEqual([{ deleted: a3, last: a2 }]);
    expect(deletedFiles([a3])).toEqual([{ deleted: a3, last: null }]);
  });

  it("lists paths newest change first", () => {
    expect(paths(revs)).toEqual(["nodes/a.py", "nodes/b.py", "views/board/App.tsx"]);
  });

  it("restores nodes and view files, not other files", () => {
    expect(restoreTarget(a2)).toEqual({ kind: "node", name: "a" });
    expect(restoreTarget(v1)).toEqual({ kind: "view", name: "board", path: "App.tsx" });
    expect(restoreTarget(rev("lib/util.py", "x"))).toBeNull();
    expect(restoreTarget(rev("AGENTS.md", "x"))).toBeNull();
  });

  it("restores a tag's file the same way as a saved version", () => {
    const file = { path: "views/board/App.tsx", node: null, view: "board", hash: "h", revision: "r", saved_at: 1, current_hash: "h2", status: "changed" as const };
    expect(restoreTarget(file)).toEqual({ kind: "view", name: "board", path: "App.tsx" });
    expect(restoreTarget({ path: "nodes/a.py", node: "a", view: null })).toEqual({ kind: "node", name: "a" });
    expect(restoreTarget({ path: "lib/x.py", node: null, view: null })).toBeNull();
  });
});
