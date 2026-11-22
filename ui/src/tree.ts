import type { GraphNode, HelperFile, ProjectView } from "./api";

/*
 * The Files mode's tree: every file the workbench can read and write (node
 * files under nodes/, helpers under lib/, view files under views/<name>/), as
 * folders and files keyed by their project-relative paths.
 */

/** One file of the project, and how the API reads and writes it. */
export type FileRef =
  | { kind: "node"; path: string; node: string }
  | { kind: "lib"; path: string }
  /** `file` is relative to the view's folder; `entry` marks the view's component file. */
  | { kind: "view"; path: string; view: string; file: string; entry: boolean };

export interface TreeDir {
  type: "dir";
  name: string;
  /** Project-relative, no trailing slash: `nodes`, `views/vol_board/components`. */
  path: string;
  /** Set on a view's own folder (`views/<name>`). */
  view?: string;
  children: TreeEntry[];
}

export interface TreeFile {
  type: "file";
  name: string;
  path: string;
  ref: FileRef;
}

export type TreeEntry = TreeDir | TreeFile;

/** Every file of the graph and the views, nodes first, then helpers, then views. */
export function projectFiles(nodes: GraphNode[], views: ProjectView[], helpers: HelperFile[]): FileRef[] {
  return [
    ...nodes.map((n): FileRef => ({ kind: "node", path: n.path, node: n.name })),
    ...helpers.map((h): FileRef => ({ kind: "lib", path: h.path })),
    ...views.flatMap((v) =>
      v.files.map((f): FileRef => ({ kind: "view", path: `${v.path}/${f}`, view: v.name, file: f, entry: f === v.entry })),
    ),
  ];
}

/** Within a folder: subfolders first, then a view's manifest and entry, then by name. */
function rank(e: TreeEntry): number {
  if (e.type === "dir") return 0;
  if (e.ref.kind === "view" && !e.ref.file.includes("/")) {
    if (e.ref.file === "view.json") return 1;
    if (e.ref.entry) return 2;
  }
  return 3;
}

function sortDir(d: TreeDir): TreeDir {
  d.children.sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  d.children.forEach((c) => c.type === "dir" && sortDir(c));
  return d;
}

/**
 * The files as folders. `nodes/` and `views/` are always there (empty without
 * a filter); `lib/` only when it holds a helper. `filter` keeps the files whose
 * path contains it (any case), and the folders on the way to them.
 */
export function buildTree(files: FileRef[], filter = ""): TreeDir[] {
  const q = filter.trim().toLowerCase();
  const roots = new Map<string, TreeDir>();
  if (!q) for (const r of ["nodes", "views"]) roots.set(r, { type: "dir", name: r, path: r, children: [] });
  const dirs = new Map<string, TreeDir>();

  for (const ref of files) {
    if (q && !ref.path.toLowerCase().includes(q)) continue;
    const parts = ref.path.split("/");
    let parent: TreeDir | null = null;
    let at = "";
    for (const part of parts.slice(0, -1)) {
      at = at ? `${at}/${part}` : part;
      let dir: TreeDir | undefined = parent ? dirs.get(at) : roots.get(at);
      if (!dir) {
        dir = { type: "dir", name: part, path: at, children: [] };
        if (ref.kind === "view" && at === `views/${ref.view}`) dir.view = ref.view;
        if (parent) parent.children.push(dir);
        else roots.set(at, dir);
      }
      dirs.set(at, dir);
      parent = dir;
    }
    const file: TreeFile = { type: "file", name: parts[parts.length - 1], path: ref.path, ref };
    if (parent) parent.children.push(file);
  }

  const order = ["nodes", "lib", "views"];
  const pos = (p: string) => (order.includes(p) ? order.indexOf(p) : order.length);
  return [...roots.values()].sort((a, b) => pos(a.path) - pos(b.path) || a.path.localeCompare(b.path)).map(sortDir);
}

/** The number of files under a folder, at any depth. */
export function countFiles(d: TreeDir): number {
  return d.children.reduce((n, c) => n + (c.type === "dir" ? countFiles(c) : 1), 0);
}

/** The folders on the way to `path`: `views/a/b.tsx` → `views`, `views/a`. */
export function parents(path: string): string[] {
  const parts = path.split("/").slice(0, -1);
  return parts.map((_, i) => parts.slice(0, i + 1).join("/"));
}
