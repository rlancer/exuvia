import { ARROW_STREAM, arrowMeta, isArrow, readArrow, toGrid } from "./arrow";

export type State = "fresh" | "stale" | "new" | "failed" | "error" | "queued" | "running";

export interface Column {
  name: string;
  type: string;
}

export interface RunRow {
  id: string;
  node: string;
  batch_id?: string;
  trigger: string;
  status: string;
  queued_at: number;
  started_at?: number | null;
  finished_at?: number | null;
  rows?: number | null;
  error?: string | null;
  log?: string;
  artifact_id?: string | null;
  /** The signed-in person who started it, or whose run cascaded into it. */
  started_by?: string | null;
}

export interface GraphNode {
  name: string;
  kind: "python" | "sql";
  path: string;
  deps: string[];
  description: string;
  cascade: boolean;
  chart: Record<string, unknown> | null;
  tags: string[];
  parse_error: string | null;
  line: number;
  state: State;
  reason: string | null;
  rows: number | null;
  /** A table's columns; for `multi`, one entry per part (`type` is `table` or `json`). */
  schema: Column[] | null;
  output_kind: "table" | "json" | "multi" | "pickle" | "none" | null;
  artifact_id: string | null;
  code_hash: string;
  finished_at: number | null;
  last_run: RunRow | null;
  schedule: { cron: string; paused: boolean; next: number | null } | null;
  /** The last run failed importing something not installed: the package that likely provides it. */
  missing_package?: { module: string; package: string } | null;
}

export interface HelperFile {
  path: string;
  used_by: string[];
}

/** One requirement in the project's pyproject.toml, and what is installed for it. */
export interface PackageRow {
  requirement: string;
  name: string;
  version: string | null;
  /** `project`: installed for this project; `exuvia`: comes with exuvia itself. */
  source: "project" | "exuvia" | null;
}

/** Packages the agent asked for, waiting for a person to approve them. */
export interface PackageRequest {
  id: number;
  packages: string[];
  reason: string;
  origin: string;
  at: number;
}

/** `GET /api/packages`: the project's packages (exuvia/packages.py Packages.state). */
export interface PackagesState {
  status: "none" | "pending" | "installing" | "ready" | "failed" | "remote";
  error: string | null;
  packages: PackageRow[];
  installed: number;
  log: string;
  requests: PackageRequest[];
  agent: "ask" | "install" | "off";
  manifest: string | null;
}

/** A view's state for one parameter set, derived from the nodes it reads. */
export type ViewState = "ready" | "missing" | "stale" | "error";

/** One named output of a node that returns several (`output_kind: "multi"`). */
export interface OutputPart {
  name: string;
  kind: "table" | "json";
  rows: number | null;
}

/** One node a view reads, and the output the view would show now. */
export interface ViewOutput {
  node: string;
  state: State;
  artifact_id: string | null;
  kind: GraphNode["output_kind"];
  /** For `kind: "multi"`: its parts, read as `node.part`. */
  parts?: OutputPart[];
}

/** A folder under views/: custom UI that only reads node outputs. */
export interface ProjectView {
  name: string;
  path: string;
  title: string;
  description: string;
  runtime: "react";
  entry: string;
  reads: string[];
  files: string[];
  hash: string;
  parse_error: string | null;
  state: ViewState;
  reason: string | null;
  outputs: ViewOutput[];
}

/** A declared run parameter and its value in the requested parameter set. */
export interface ProjectParam {
  name: string;
  type: "date" | "string" | "number" | "boolean";
  default: unknown;
  description?: string;
  value: unknown;
}

export interface Project {
  name: string;
  root: string;
  params: ProjectParam[];
  nodes: GraphNode[];
  views: ProjectView[];
}

/** `GET /api/views/{name}`: a view with every file's source. */
export interface ViewSources extends ProjectView {
  manifest: Record<string, unknown> | null;
  sources: { path: string; source: string }[];
}

/** A node's output as a view reads it: Arrow bytes of a table, or a JSON value. */
export type ViewData = { kind: "table"; arrow: ArrayBuffer } | { kind: "json"; value: unknown } | { kind: null };

/** One saved version of a code file (`GET /api/code/revisions`; exuvia/store.py code_revisions). */
export interface CodeRevision {
  id: number;
  /** Project-relative, e.g. `nodes/prices.py` or `views/board/App.tsx`. */
  path: string;
  node: string | null;
  view: string | null;
  /** The text's content hash; null when this revision deleted the file. */
  hash: string | null;
  /** What saved it: `ui`, `agent`, `api`, `file`, `template`, `restore`, `load`, `import`, … */
  origin: string;
  author: string | null;
  /** Unix seconds. */
  saved_at: number;
  /** This version is the file's text now. */
  current: boolean;
}

export interface Preview {
  kind: "table" | "json" | "multi" | "pickle" | "none" | null;
  /** For `kind: "multi"`: the parts, each read with `part`. */
  parts?: OutputPart[];
  columns?: Column[];
  rows?: unknown[][];
  total?: number | null;
  value?: unknown;
}

/**
 * Where this page's project API lives. `exuvia serve` serves one project at
 * `/` with its API at `/api`; a hub serves each project's workbench at
 * `/p/<id>/` with its API at `/p/<id>/api`.
 */
/** The hub project this page is the workbench of, or null under `exuvia serve`. */
export const HUB_PROJECT = (() => {
  const m = typeof location === "undefined" ? null : /^\/p\/([^/]+)\//.exec(location.pathname);
  return m ? decodeURIComponent(m[1]) : null;
})();

export const API_BASE = HUB_PROJECT ? `/p/${encodeURIComponent(HUB_PROJECT)}/api` : "/api";

/** A project API path written as `/api/...`, resolved against {@link API_BASE}. */
export const apiUrl = (path: string) => (path.startsWith("/api/") ? API_BASE + path.slice(4) : path);

/** An error answer, with the server's own message and the HTTP status. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

/**
 * Send one request to a project API path; an error answer is thrown as an {@link ApiError}.
 * Code it changes is recorded as saved from the `ui` (the revision's `origin`), not `api`.
 */
function send(method: string, url: string, body?: unknown, accept?: string): Promise<Response> {
  return sendTo(method, apiUrl(url), body, accept, undefined, UI_ORIGIN);
}

/** The `X-Exuvia-Origin` the workbench sends: revisions it saves say `ui`. */
export const UI_ORIGIN = "ui";

async function sendTo(
  method: string,
  url: string,
  body?: unknown,
  accept?: string,
  auth?: string,
  origin?: string,
): Promise<Response> {
  const headers: Record<string, string> = {};
  if (accept) headers.Accept = accept;
  if (auth) headers.Authorization = `Bearer ${auth}`;
  if (origin) headers["X-Exuvia-Origin"] = origin;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      msg = (await res.json()).detail ?? msg;
    } catch {
      /* not json */
    }
    throw new ApiError(msg, res.status);
  }
  return res;
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  return (await send(method, url, body)).json() as Promise<T>;
}

/** A node's output: the first `limit` rows of a table (read as Arrow), or its JSON value.
 * `part` picks one output of a node with several. */
async function nodeData(name: string, limit: number, part?: string): Promise<Preview> {
  const q = `limit=${limit}${part ? `&part=${encodeURIComponent(part)}` : ""}`;
  const res = await send("GET", `/api/nodes/${name}/data?${q}`, undefined, `${ARROW_STREAM}, application/json`);
  if (!isArrow(res)) return res.json() as Promise<Preview>;
  const table = await readArrow(res);
  return { kind: "table", total: arrowMeta(table).total ?? table.numRows, ...toGrid(table) };
}

/** DuckDB SQL over every output; the first `limit` rows of the result (read as Arrow). */
async function queryRows(sql: string, limit = 500): Promise<{ columns: Column[]; rows: unknown[][] }> {
  return toGrid(await readArrow(await send("POST", "/api/query", { sql, limit }, ARROW_STREAM)));
}

const paramsQuery = (params?: Record<string, unknown>) =>
  params && Object.keys(params).length ? `?params=${encodeURIComponent(JSON.stringify(params))}` : "";

/** A node's output through the view-scoped route (403 unless the view reads it);
 * `part` picks one output of a node with several. */
async function viewData(view: string, node: string, params?: Record<string, unknown>, part?: string): Promise<ViewData> {
  const q = new URLSearchParams();
  if (params && Object.keys(params).length) q.set("params", JSON.stringify(params));
  if (part) q.set("part", part);
  const qs = q.toString();
  const url = `/api/views/${view}/data/${encodeURIComponent(node)}${qs ? `?${qs}` : ""}`;
  const res = await send("GET", url, undefined, `${ARROW_STREAM}, application/json`);
  if (isArrow(res)) return { kind: "table", arrow: await res.arrayBuffer() };
  const body = (await res.json()) as Preview;
  if (body.kind === "json") return { kind: "json", value: body.value };
  if (body.kind === "table") throw new Error("expected an Arrow answer");
  return { kind: null };
}

export const api = {
  config: () => req<{ ai_enabled: boolean; model: string; project: string }>("GET", "/api/config"),
  project: (params?: Record<string, unknown>) => req<Project>("GET", `/api/project${paramsQuery(params)}`),
  view: (name: string) => req<ViewSources>("GET", `/api/views/${name}`),
  /** Write one file of a view (`App.tsx`, `components/Chart.tsx`, `view.json`); answers the view as listed. */
  saveViewFile: (name: string, path: string, source: string) =>
    req<ProjectView>("PUT", `/api/views/${name}/files/${path.split("/").map(encodeURIComponent).join("/")}`, { source }),
  /** The text of one file of a view; `path` is relative to the view's folder. */
  viewFile: async (name: string, path: string) =>
    (await req<{ source: string }>("GET", `/api/views/${name}/files/${path.split("/").map(encodeURIComponent).join("/")}`)).source,
  viewData,
  /** DuckDB SQL that sees only the view's `reads`; the whole result as Arrow bytes. */
  viewQuery: async (view: string, sql: string, params?: Record<string, unknown>) =>
    (await send("POST", `/api/views/${view}/query`, { sql, params: params ?? null }, ARROW_STREAM)).arrayBuffer(),
  node: (name: string) => req<{ name: string; source: string }>("GET", `/api/nodes/${name}`),
  helpers: () => req<{ files: HelperFile[] }>("GET", "/api/files"),
  helper: (path: string) =>
    req<HelperFile & { source: string }>("GET", `/api/files/${path.split("/").map(encodeURIComponent).join("/")}`),
  /** Write a helper (`lib/stats.py`); `parse_error` is set when it is not valid Python (it is written all the same). */
  saveHelper: (path: string, source: string) =>
    req<HelperFile & { parse_error: string | null }>("PUT", `/api/files/${path.split("/").map(encodeURIComponent).join("/")}`, { source }),
  save: (name: string, source: string) => req<Project>("PUT", `/api/nodes/${name}`, { source }),
  create: (b: { name: string; kind: string; deps: string[]; description: string }) =>
    req<Project>("POST", "/api/nodes", b),
  remove: (name: string) => req<Project>("DELETE", `/api/nodes/${name}`),
  run: (name: string, force = true) => req("POST", `/api/nodes/${name}/run`, { force }),
  /** Bring `targets` up to date (stale ancestors included) for one parameter set. */
  runWith: (targets: string[], params: Record<string, unknown>) =>
    req("POST", "/api/run", { targets, force: false, params: Object.keys(params).length ? params : null }),
  runStale: () => req<{ targets: string[] }>("POST", "/api/run-stale"),
  pause: (name: string, paused: boolean) => req("POST", `/api/nodes/${name}/schedule`, { paused }),
  data: (name: string, limit = 200, part?: string) => nodeData(name, limit, part),
  runs: (name: string) => req<RunRow[]>("GET", `/api/nodes/${name}/runs`),
  recentRuns: () => req<RunRow[]>("GET", "/api/runs"),
  runDetail: (id: string) => req<RunRow>("GET", `/api/runs/${id}`),
  cancel: (id: string) => req("POST", `/api/runs/${id}/cancel`),
  query: (sql: string) => queryRows(sql),
  resetAgent: (id: string) => req("DELETE", `/api/agent/${id}`),
  /** Saved versions newest first: one node's, one view's files (deleted ones too), or every file's. */
  codeRevisions: (f: { node?: string; view?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (f.node) q.set("node", f.node);
    if (f.view) q.set("view", f.view);
    if (f.limit) q.set("limit", String(f.limit));
    const qs = q.toString();
    return req<CodeRevision[]>("GET", `/api/code/revisions${qs ? `?${qs}` : ""}`);
  },
  /** The text of one saved version. */
  codeBlob: async (hash: string) => (await req<{ hash: string; source: string }>("GET", `/api/code/blobs/${hash}`)).source,
  /** Make a saved version of a node current again (a deleted node comes back). */
  restoreNode: (name: string, hash: string) => req<Project>("POST", `/api/nodes/${name}/restore`, { hash }),
  /** The same for one file of a view; `path` is relative to the view's folder. */
  restoreViewFile: (name: string, path: string, hash: string) =>
    req<ProjectView>("POST", `/api/views/${name}/restore`, { path, hash }),
  packages: () => req<PackagesState>("GET", "/api/packages"),
  /** Add requirements and/or remove packages; a list that does not resolve is an error and changes nothing. */
  packagesEdit: (b: { add?: string[]; remove?: string[] }) => req<PackagesState>("POST", "/api/packages", b),
  packagesManifest: (text: string) => req<PackagesState>("PUT", "/api/packages/manifest", { text }),
  packagesRetry: () => req<PackagesState>("POST", "/api/packages/retry"),
  packagesApprove: (id: number) => req<PackagesState>("POST", `/api/packages/requests/${id}/approve`),
  packagesDismiss: (id: number) => req<PackagesState>("DELETE", `/api/packages/requests/${id}`),
};

/** Who this page is signed in as (`GET /api/me`, on the server itself, not under a project). */
export interface Me {
  /** How the server identifies people: a sign-in proxy's `headers`, a shared `token`, or `none`. */
  auth: "headers" | "token" | "none";
  user: {
    /** The name records show: the user name, else the email. */
    id: string;
    user: string | null;
    email: string | null;
    method: string;
    /** The stable user id in the user list. */
    uid: string | null;
    role: "admin" | "member" | null;
  } | null;
  /** Whether people can make agent tokens here (a server with a user list). */
  tokens?: boolean;
}

export const me = async () => (await sendTo("GET", "/api/me")).json() as Promise<Me>;

/** One of the signed-in person's agent tokens: never the token itself. */
export interface AgentToken {
  id: string;
  name: string;
  /** Its last 4 characters. */
  hint: string;
  created_at: number;
  last_used: number | null;
}

/** The signed-in person's agent tokens (`/api/me/tokens`, not under a project). */
export const tokensApi = {
  list: async () => ((await sendTo("GET", "/api/me/tokens")).json() as Promise<{ tokens: AgentToken[] }>).then((r) => r.tokens),
  /** A new token; `token` is in this answer only. */
  create: async (name: string) =>
    (await sendTo("POST", "/api/me/tokens", { name })).json() as Promise<AgentToken & { token: string }>,
  revoke: async (id: string) => {
    await sendTo("DELETE", `/api/me/tokens/${encodeURIComponent(id)}`);
  },
};

/** `GET /api/about`: the running version, how it was installed and whether a newer release is out (exuvia/about.py). */
export interface About {
  version: string;
  install: "deployed" | "source" | "uvx" | "uv-tool" | "pipx" | "pip";
  deployed: boolean;
  /** The command that updates this install; null on a deployed server. */
  upgrade: string | null;
  image: string;
  latest: string | null;
  update: "off" | "dev" | "error" | "available" | "current";
  error: string | null;
  /** Unix seconds. */
  checked_at: number | null;
  changes_url: string | null;
  repo_url: string;
  docs_url: string;
  issues_url: string;
}

export const about = async (refresh = false) =>
  (await sendTo("GET", refresh ? "/api/about?refresh=true" : "/api/about")).json() as Promise<About>;

/** Someone in the user list (`GET /api/users`, admins only). */
export interface UserRow {
  id: string;
  name: string;
  email: string | null;
  role: "admin" | "member";
  disabled: boolean;
  /** Named in EXUVIA_ADMINS: always an admin, never disabled. */
  pinned_admin: boolean;
  created_at: number;
  last_seen: number;
  identities: { method: string; subject: string; email: string | null; created_at: number; last_seen: number }[];
}

/** The server's user list (served at `/api/users` in both shapes, not under a project). */
export const usersApi = {
  list: async () => ((await sendTo("GET", "/api/users")).json() as Promise<{ users: UserRow[] }>).then((r) => r.users),
  update: async (id: string, change: { role?: "admin" | "member"; disabled?: boolean }) =>
    (await sendTo("PATCH", `/api/users/${encodeURIComponent(id)}`, change)).json() as Promise<UserRow>,
};

/** A project on a hub, as `GET /api/projects` lists it. */
export interface HubProject {
  id: string;
  name: string;
  nodes: number;
  loaded: boolean;
}

/** Project ids a hub accepts (exuvia/hub.py PROJECT_ID). */
export const PROJECT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

/** The hub's own API (`/api/projects`), for the hub's home page at `/`. */
export const hubApi = {
  /** Every project and template; `token` checks a token before the browser keeps it. */
  projects: async (token?: string) =>
    (await sendTo("GET", "/api/projects", undefined, undefined, token)).json() as Promise<{ projects: HubProject[]; templates: string[]; auth: boolean }>,
  create: async (b: { id: string; name?: string; template?: string }) =>
    (await sendTo("POST", "/api/projects", b)).json() as Promise<{ id: string; name: string }>,
  remove: async (id: string) => {
    await sendTo("DELETE", `/api/projects/${encodeURIComponent(id)}`);
  },
};

export type AgentEvent =
  | { type: "session"; session_id: string }
  | { type: "thinking" }
  | { type: "text"; text: string }
  | { type: "tool_start"; id: string; name: string }
  | { type: "tool"; id: string; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; ok: boolean; summary: string; node?: string }
  | { type: "error"; error: string }
  | { type: "done" };

/** POST + read an SSE body (EventSource only does GET). */
export async function streamAgent(
  body: { message: string; session_id: string | null; selected: string | null },
  onEvent: (e: AgentEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(apiUrl("/api/agent"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    let msg = res.statusText;
    try {
      msg = (await res.json()).detail ?? msg;
    } catch {
      /* ignore */
    }
    onEvent({ type: "error", error: msg });
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
      }
    }
  }
}
