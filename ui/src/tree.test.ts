import { describe, expect, it } from "vitest";
import type { GraphNode, ProjectView } from "./api";
import { buildTree, countFiles, parents, projectFiles, type TreeDir, type TreeEntry } from "./tree";

const node = (name: string, path: string) => ({ name, path }) as GraphNode;
const view = (name: string, files: string[], entry = "App.tsx") => ({ name, path: `views/${name}`, files, entry }) as ProjectView;

/** A tree as nested names, folders with a trailing slash. */
const shape = (es: TreeEntry[]): unknown[] =>
  es.map((e) => (e.type === "dir" ? { [`${e.name}/`]: shape(e.children) } : e.name));

const files = projectFiles(
  [node("returns", "nodes/returns.py"), node("prices", "nodes/prices.py"), node("summary", "nodes/summary.sql")],
  [view("board", ["styles.css", "App.tsx", "components/Chart.tsx", "view.json"])],
  [{ path: "lib/vol/surface.py", used_by: [] }, { path: "lib/stats.py", used_by: ["returns"] }],
);

describe("projectFiles", () => {
  it("lists nodes, helpers and view files with how to reach each", () => {
    expect(files.map((f) => f.path)).toEqual([
      "nodes/returns.py",
      "nodes/prices.py",
      "nodes/summary.sql",
      "lib/vol/surface.py",
      "lib/stats.py",
      "views/board/styles.css",
      "views/board/App.tsx",
      "views/board/components/Chart.tsx",
      "views/board/view.json",
    ]);
    expect(files[0]).toEqual({ kind: "node", path: "nodes/returns.py", node: "returns" });
    expect(files[6]).toEqual({ kind: "view", path: "views/board/App.tsx", view: "board", file: "App.tsx", entry: true });
  });
});

describe("buildTree", () => {
  it("nests files in folders: nodes, lib, views; folders first, then a view's manifest and entry", () => {
    expect(shape(buildTree(files))).toEqual([
      { "nodes/": ["prices.py", "returns.py", "summary.sql"] },
      { "lib/": [{ "vol/": ["surface.py"] }, "stats.py"] },
      { "views/": [{ "board/": [{ "components/": ["Chart.tsx"] }, "view.json", "App.tsx", "styles.css"] }] },
    ]);
  });

  it("marks a view's own folder", () => {
    const views = buildTree(files)[2];
    const board = views.children[0] as TreeDir;
    expect(board.view).toBe("board");
    expect((board.children[0] as TreeDir).view).toBeUndefined();
    expect(countFiles(views)).toBe(4);
  });

  it("keeps nodes/ and views/ when empty, and lib/ only with a helper", () => {
    expect(shape(buildTree([]))).toEqual([{ "nodes/": [] }, { "views/": [] }]);
  });

  it("filters by path, any case, keeping the folders on the way", () => {
    expect(shape(buildTree(files, "CHART"))).toEqual([{ "views/": [{ "board/": [{ "components/": ["Chart.tsx"] }] }] }]);
    expect(shape(buildTree(files, "S.PY"))).toEqual([{ "nodes/": ["prices.py", "returns.py"] }, { "lib/": ["stats.py"] }]);
    expect(buildTree(files, "nothing-like-it")).toEqual([]);
  });
});

describe("parents", () => {
  it("lists the folders on the way to a file", () => {
    expect(parents("views/board/components/Chart.tsx")).toEqual(["views", "views/board", "views/board/components"]);
    expect(parents("nodes/a.py")).toEqual(["nodes"]);
  });
});
