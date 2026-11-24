/**
 * The postMessage protocol between a view's sandboxed iframe and the exuvia
 * page that hosts it. Both sides import this module; nothing else crosses
 * the frame boundary.
 *
 * The iframe is `sandbox="allow-scripts"` (an opaque origin: no cookies, no
 * token, no same-origin access) with a CSP that forbids every network request,
 * so every byte it gets, the runtime code included, comes through these
 * messages, and the host only answers for the nodes the view lists in `reads`.
 *
 *   iframe → host   boot                        the frame's bootstrap is listening
 *   host → iframe   runtime {source}            the runtime script; the bootstrap evaluates it
 *   iframe → host   ready                       runtime loaded, send the view
 *   host → iframe   load {view, params, dark}   compile `view.entry` and render it
 *   iframe → host   request {id, method, args}  table | json | query | params | setParams | download
 *   host → iframe   response {id, ok, ...}      the answer (Arrow bytes are transferred)
 *   host → iframe   changed {nodes, params}     outputs the view reads were replaced
 *   host → iframe   theme {dark}
 *   iframe → host   status {phase, ...}         compiled / rendered / an error with its file
 *
 * Every message carries `channel: CHANNEL`; each side also checks the
 * sender (`event.source`), since the iframe's origin is the string "null".
 */

export const CHANNEL = "exuvia-view/1";

/** The npm modules a view imports, as the page sends them (exuvia/npm.py `Npm.modules`). */
export interface PackageModules {
  /** The packages the project's package.json lists. */
  declared: string[];
  /** Bare import (`recharts`, `lodash-es/debounce`) → the id of its module. */
  entries: Record<string, string>;
  /** Module id (its path on the CDN) → source. */
  modules: Record<string, string>;
  /** Bare import → why its modules could not be fetched. */
  errors: Record<string, string>;
  /** Why none could be fetched (the request failed). */
  error?: string;
}

export const NO_PACKAGES: PackageModules = { declared: [], entries: {}, modules: {}, errors: {} };

/** Run-parameter values by name, as `GET /api/project` resolves them. */
export type Params = Record<string, unknown>;

/** A declared run parameter as the host knows it. */
export interface ParamSpec {
  name: string;
  type?: string;
  description?: string;
  default?: unknown;
  value: unknown;
}

/** What the iframe gets to run: the view's own files and the npm modules it imports, nothing else. */
export interface ViewBundle {
  name: string;
  title: string;
  entry: string;
  reads: string[];
  /** Relative path (inside the view folder) → source text. */
  files: Record<string, string>;
  /** The project's npm packages the view imports (loader.ts). */
  packages?: PackageModules;
}

export type Method = "table" | "json" | "query" | "params" | "setParams" | "download";

/** The file formats a view can save a table in. */
export type DownloadFormat = "csv" | "xlsx" | "parquet" | "json";
export const DOWNLOAD_FORMATS: readonly DownloadFormat[] = ["csv", "xlsx", "parquet", "json"];

export interface RequestArgs {
  table: { node: string };
  json: { node: string };
  query: { sql: string };
  params: Record<string, never>;
  setParams: { params: Params };
  /** Save a read node's output (`node`) or a query's whole result (`sql`) as a file. */
  download: { node?: string; sql?: string; format: DownloadFormat; filename?: string };
}

export interface ParamsState {
  params: Params;
  specs: ParamSpec[];
}

export interface ResultOf {
  /** Arrow IPC stream bytes of the node's current table. */
  table: ArrayBuffer;
  /** The node's current JSON output. */
  json: unknown;
  /** Arrow IPC stream bytes of the query result. */
  query: ArrayBuffer;
  params: ParamsState;
  setParams: ParamsState;
  download: null;
}

/** Where a view failed: compiling its files, or while running. */
export type ErrorPhase = "compile" | "runtime";

export type ToHost =
  | { channel: typeof CHANNEL; type: "boot" }
  | { channel: typeof CHANNEL; type: "ready" }
  | { [M in Method]: { channel: typeof CHANNEL; type: "request"; id: number; method: M; args: RequestArgs[M] } }[Method]
  | { channel: typeof CHANNEL; type: "status"; phase: "compiled" | "rendered" }
  | { channel: typeof CHANNEL; type: "status"; phase: ErrorPhase; error: string; file?: string; stack?: string };

export type ToFrame =
  | { channel: typeof CHANNEL; type: "runtime"; source: string }
  | { channel: typeof CHANNEL; type: "load"; view: ViewBundle; params: ParamsState; dark: boolean }
  | { channel: typeof CHANNEL; type: "response"; id: number; ok: true; result: unknown }
  | { channel: typeof CHANNEL; type: "response"; id: number; ok: false; error: string }
  | { channel: typeof CHANNEL; type: "changed"; nodes: string[]; params: ParamsState }
  | { channel: typeof CHANNEL; type: "theme"; dark: boolean };

/** Distributes `Omit` over a union, so each member keeps its own fields. */
type Bare<T> = T extends unknown ? Omit<T, "channel"> : never;
export type ToHostBody = Bare<ToHost>;
export type ToFrameBody = Bare<ToFrame>;

const METHODS: ReadonlySet<string> = new Set<Method>(["table", "json", "query", "params", "setParams", "download"]);

function tagged(data: unknown): data is { channel: string; type: string } {
  return typeof data === "object" && data !== null && (data as { channel?: unknown }).channel === CHANNEL;
}

/** A well-formed message from a view frame (the host still checks the sender). */
export function isToHost(data: unknown): data is ToHost {
  if (!tagged(data)) return false;
  const d = data as Record<string, unknown>;
  switch (d.type) {
    case "boot":
    case "ready":
      return true;
    case "status":
      return typeof d.phase === "string";
    case "request": {
      if (!Number.isInteger(d.id) || typeof d.method !== "string" || !METHODS.has(d.method)) return false;
      const a = d.args as Record<string, unknown> | null;
      if (typeof a !== "object" || a === null) return false;
      if (d.method === "table" || d.method === "json") return typeof a.node === "string";
      if (d.method === "query") return typeof a.sql === "string";
      if (d.method === "setParams") return typeof a.params === "object" && a.params !== null && !Array.isArray(a.params);
      if (d.method === "download")
        return (
          (typeof a.node === "string") !== (typeof a.sql === "string") &&
          (DOWNLOAD_FORMATS as readonly unknown[]).includes(a.format) &&
          (a.filename === undefined || typeof a.filename === "string")
        );
      return true;
    }
    default:
      return false;
  }
}

export function isToFrame(data: unknown): data is ToFrame {
  return tagged(data) && ["runtime", "load", "response", "changed", "theme"].includes(data.type);
}
