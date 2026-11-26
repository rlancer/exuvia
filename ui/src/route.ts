/**
 * The workbench's address (the part after `#`), read and written in one
 * place. Pure, so it is tested on its own (route.test.ts).
 *
 *   (none)               the graph
 *   #notebook            the graph read as a notebook; #notebook/<node> on that node's cell
 *   #views               the views; #views/<name> that view, rendered
 *   #history             the code's history
 *   #schedule            when the graph runs by itself
 *   #files/<path>        a file where it lives: a node on the graph, a view's
 *                        file in its Code tab, a helper (lib/…) in the editor
 *   #env, #env=<NAME>    the Environment dialog (on that variable)
 */
export type Route =
  | { page: "graph" }
  | { page: "notebook"; node: string | null }
  | { page: "views"; view: string | null }
  | { page: "history" }
  | { page: "schedule" }
  | { page: "file"; path: string }
  | { page: "env"; name: string | null };

const NAME = "[a-z_][a-z0-9_]*";

/** The route an address names; anything else (or nothing) is the graph. */
export function parse(hash: string): Route {
  const h = hash.startsWith("#") ? hash : `#${hash}`;
  let m: RegExpExecArray | null;
  if ((m = new RegExp(`^#notebook(?:/(${NAME}))?$`).exec(h))) return { page: "notebook", node: m[1] ?? null };
  if ((m = new RegExp(`^#views(?:/(${NAME}))?$`).exec(h))) return { page: "views", view: m[1] ?? null };
  if (h === "#history") return { page: "history" };
  if (h === "#schedule") return { page: "schedule" };
  if ((m = /^#env(?:=([A-Za-z_][A-Za-z0-9_]{0,127}))?$/.exec(h))) return { page: "env", name: m[1] ?? null };
  if ((m = /^#files\/(.+)$/.exec(h))) {
    try {
      return { page: "file", path: decodeURI(m[1]) };
    } catch {
      /* a malformed address */
    }
  }
  return { page: "graph" };
}

/** The address of a route: `""` for the graph, which has none of its own. */
export function format(r: Route): string {
  switch (r.page) {
    case "graph":
      return "";
    case "notebook":
      return r.node ? `#notebook/${r.node}` : "#notebook";
    case "views":
      return r.view ? `#views/${r.view}` : "#views";
    case "history":
    case "schedule":
      return `#${r.page}`;
    case "file":
      return `#files/${encodeURI(r.path)}`;
    case "env":
      return r.name ? `#env=${r.name}` : "#env";
  }
}

/** What the main area shows: the graph, the views, the schedule, a shared helper, or the code's history. */
export type Mode = "graph" | "views" | "schedule" | "helper" | "history";
/** How Graph mode shows the graph: drawn on the canvas, or read as a notebook. */
export type Layout = "canvas" | "notebook";

/** Where the workbench opens for a route: its mode, and the node, view (and file) or helper it opens on. */
export interface Landing {
  mode: Mode;
  layout?: Layout;
  node?: string;
  view?: { view: string; file?: string };
  helper?: string;
}

export function landing(r: Route): Landing {
  switch (r.page) {
    case "notebook":
      return { mode: "graph", layout: "notebook", ...(r.node ? { node: r.node } : {}) };
    case "views":
      return r.view ? { mode: "views", view: { view: r.view } } : { mode: "views" };
    case "history":
    case "schedule":
      return { mode: r.page };
    case "file": {
      const node = /^nodes\/([^/]+)\.(py|sql)$/.exec(r.path);
      if (node) return { mode: "graph", node: node[1] };
      const view = /^views\/([^/]+)\/(.+)$/.exec(r.path);
      if (view) return { mode: "views", view: { view: view[1], file: view[2] } };
      if (r.path.startsWith("lib/")) return { mode: "helper", helper: r.path };
      return { mode: "graph" };
    }
    default:
      return { mode: "graph" };
  }
}

/** The address a mode is left at: the views and history lists, the schedule, the open helper, or the graph's. */
export function routeOf(mode: Mode, helper: string | null): Route {
  switch (mode) {
    case "views":
    case "history":
    case "schedule":
      return mode === "views" ? { page: "views", view: null } : { page: mode };
    case "helper":
      return helper ? { page: "file", path: helper } : { page: "graph" };
    default:
      return { page: "graph" };
  }
}
