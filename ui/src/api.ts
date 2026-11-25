import { ARROW_STREAM, arrowMeta, isArrow, readArrow, toGrid } from "./arrow";
import type { SavedEvent } from "./chat";
import type { PackageModules } from "./views/protocol";

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

/** One scheduled node in `GET /api/schedule`; times are Unix seconds. */
export type ScheduleRow = {
  name: string;
  cron: string;
  /** `code` (the node's file) or `config` (exuvia.json `schedules`). */
  source: string | null;
  paused: boolean;
  overlap: string;
  /** Null while paused or while its code is broken. */
  next: number | null;
  upcoming: number[];
  /** Its latest run started by the clock. */
  last: RunRow | null;
  /** What a new output from it re-runs. */
  cascades: string[];
  state: State | null;
  parse_error: string | null;
};

/** A live node in `GET /api/schedule`: it runs when a view that depends on it opens. */
export type LiveRow = {
  name: string;
  /** 0: every open; else the seconds an output stays recent enough. */
  live: number;
  views: string[];
  last: RunRow | null;
  state: State | null;
};

/** `GET /api/schedule`: when the graph runs by itself. */
export interface ScheduleState {
  /** The clock crons are read on; null is the server's local time. */
  timezone: string | null;
  scheduler: "internal" | "external";
  nodes: ScheduleRow[];
  live: LiveRow[];
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
  /** Re-run when a view that depends on it is opened: null when not live, else the seconds an output stays recent enough. */
  live?: number | null;
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

/** One npm package the project's views may import, from its package.json. */
export interface NpmPackage {
  name: string;
  /** As package.json has it: an exact version, or a range such as `^4.1.0`. */
  version: string;
  exact: boolean;
}

/** `GET /api/npm`: the npm packages for views (exuvia/npm.py Npm.state). */
export interface NpmState {
  packages: NpmPackage[];
  /** Why package.json could not be read. */
  error: string | null;
  /** What every view has without adding it. */
  builtins: string[];
  /** Where the modules are fetched from. */
  cdn: string;
}

/** One of the project's environment variables (`GET /api/env`; exuvia/envvars.py).
 * A secret's `value` is always null: it is never sent back once saved. */
export type EnvVar = {
  name: string;
  secret: boolean;
  value: string | null;
  /** False for a secret this server cannot decrypt (its key is gone): it has to be entered again. */
  readable: boolean;
  updated_at: number | null;
  updated_by: string | null;
};

/** A request to set a secret, from the agent or an MCP client (`request_secret`): its name and why, never a value. */
export type EnvRequest = { name: string; reason: string; origin: string; at: number };

/** A variable a hub project gets from its group or from the hub itself (exuvia/groups.py), read-only here. */
export type InheritedVar = EnvVar &
  (
    | { source: "group"; group: GroupRef }
    | { source: "hub" }
  ) & {
    /** A closer layer sets the same name and wins: the project, or for a hub variable the project's group. */
    overridden: boolean;
  };

/** A hub group, as a project or a variable names it. */
export type GroupRef = { id: string; name: string };

/** `GET /api/env`: the project's own variables, the requests to set one that
 * wait, and on a hub the group the project is in and what it and the hub give. */
export type EnvState = {
  variables: EnvVar[];
  requests: EnvRequest[];
  group?: GroupRef | null;
  inherited?: InheritedVar[];
};

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
  /** The live nodes it depends on (`live=True`): opening the view re-runs them (`api.refreshView`). */
  live: string[];
}

/** `POST /api/views/{name}/refresh`: the view's live nodes, those re-run now, and why the others were not. */
export interface ViewRefresh {
  live: string[];
  started: string[];
  skipped: Record<string, string>;
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
  /** The npm modules a view imports, for its frame (see views/loader.ts). */
  viewPackages: (name: string) => req<PackageModules>("GET", `/api/views/${name}/packages`),
  /** Re-run the live nodes a view depends on, for one parameter set; returns once they are queued. */
  refreshView: (name: string, params: Record<string, unknown>) =>
    req<ViewRefresh>("POST", `/api/views/${name}/refresh`, { params: Object.keys(params).length ? params : null }),
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
  schedule: () => req<ScheduleState>("GET", "/api/schedule"),
  pause: (name: string, paused: boolean) => req("POST", `/api/nodes/${name}/schedule`, { paused }),
  data: (name: string, limit = 200, part?: string) => nodeData(name, limit, part),
  runs: (name: string) => req<RunRow[]>("GET", `/api/nodes/${name}/runs`),
  recentRuns: () => req<RunRow[]>("GET", "/api/runs"),
  runDetail: (id: string) => req<RunRow>("GET", `/api/runs/${id}`),
  cancel: (id: string) => req("POST", `/api/runs/${id}/cancel`),
  query: (sql: string) => queryRows(sql),
  resetAgent: (id: string) => req("DELETE", `/api/agent/${id}`),
  /** The saved conversations, the latest first (a signed-in person's own). */
  agentChats: (limit = 50) => req<{ sessions: AgentChat[] }>("GET", `/api/agent?limit=${limit}`).then((r) => r.sessions),
  /** One saved conversation's transcript, and whether a turn is running in it. */
  agentChat: (id: string) =>
    req<{ session_id: string; running: boolean; events: SavedEvent[] }>("GET", `/api/agent/${encodeURIComponent(id)}`),
  /** Stop the turn running in a conversation. */
  cancelAgent: (id: string) => req<{ ok: boolean }>("POST", `/api/agent/${encodeURIComponent(id)}/cancel`),
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
  npm: () => req<NpmState>("GET", "/api/npm"),
  /** Add packages (each checked on the CDN; one without a version is pinned) and/or remove them. */
  npmEdit: (b: { add?: string[]; remove?: string[] }) => req<NpmState>("POST", "/api/npm", b),
  env: () => req<EnvState>("GET", "/api/env"),
  /** Add or change a variable; without `value` the saved one is kept (to make a plain variable secret). */
  setEnv: (name: string, b: { value?: string; secret: boolean }) =>
    req<EnvState>("PUT", `/api/env/${encodeURIComponent(name)}`, b),
  removeEnv: (name: string) => req<EnvState>("DELETE", `/api/env/${encodeURIComponent(name)}`),
  /** Turn down a request to set a secret (the agent's request_secret). */
  dismissEnvRequest: (name: string) => req<EnvState>("DELETE", `/api/env/requests/${encodeURIComponent(name)}`),
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

/** One of the MCP servers the built-in agent connects to for you: never the header values. */
export interface AgentMcpServer {
  name: string;
  url: string;
  enabled: boolean;
  /** Each header's name and its value's last 4 characters (empty for a short value). */
  headers: { name: string; hint: string }[];
  /** Whether you have signed in to it with OAuth (the tokens stay on the server). */
  signed_in: boolean;
  created_at: number;
  updated_at: number;
}

/** `GET /api/me/mcp`. `shared`: nobody signs in here, so everyone shares one list. */
export interface AgentMcpList {
  servers: AgentMcpServer[];
  shared: boolean;
  /** Whether a server may be on a private address (this machine, the local network). */
  private: boolean;
}

/** A connection's outcome. `signin`: the server wants an OAuth sign-in (`agentMcpApi.signIn`). */
export type AgentMcpTest =
  | { ok: true; tools: { name: string; description: string }[] }
  | { ok: false; error: string; signin?: boolean };

/** What the OAuth callback page posts to the window that opened it (exuvia/server.py). */
export type AgentMcpSignedIn = AgentMcpTest & { type: "exuvia-mcp-oauth"; name: string };

/** Your MCP servers for the built-in agent (`/api/me/mcp`, not under a project; exuvia/agent_mcp.py). */
export const agentMcpApi = {
  list: async () => (await sendTo("GET", "/api/me/mcp")).json() as Promise<AgentMcpList>,
  /** Add or change one. `headers` is merged: a value sets a header, null removes it, one left out keeps its value. */
  put: async (name: string, body: { url?: string; headers?: Record<string, string | null>; enabled?: boolean }) =>
    (await sendTo("PUT", `/api/me/mcp/${encodeURIComponent(name)}`, body)).json() as Promise<AgentMcpServer>,
  remove: async (name: string) => {
    await sendTo("DELETE", `/api/me/mcp/${encodeURIComponent(name)}`);
  },
  test: async (name: string) => (await sendTo("POST", `/api/me/mcp/${encodeURIComponent(name)}/test`)).json() as Promise<AgentMcpTest>,
  /**
   * Start an OAuth sign-in: `{url}`, the authorization server's page to open, which comes back to
   * `<origin>/api/me/mcp/oauth/callback`; or the connection's outcome when no sign-in was needed.
   * `back`: the page that callback returns to when the sign-in runs in this tab (no popup).
   */
  signIn: async (name: string, origin: string, back: string) =>
    (await sendTo("POST", `/api/me/mcp/${encodeURIComponent(name)}/oauth`, { origin, back })).json() as Promise<
      { url: string } | AgentMcpTest
    >,
  signOut: async (name: string) => {
    await sendTo("DELETE", `/api/me/mcp/${encodeURIComponent(name)}/oauth`);
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

/** A project role on a hub with sharing (exuvia/sharing.py): viewers read, editors change, owners share. */
export type Role = "viewer" | "editor" | "owner";
/** What everyone else signed in on the hub gets on a project. */
export type GeneralAccess = "private" | "view" | "edit";

/** Someone in the user list, as sharing shows them. */
export interface Person {
  id: string | null;
  name: string;
  email: string | null;
  disabled?: boolean;
}

/** A project on a hub, as `GET /api/projects` lists it. */
export interface HubProject {
  id: string;
  name: string;
  nodes: number;
  loaded: boolean;
  /** With sharing on: the caller's role, the owner, general access, and whether it is shared by name or link. */
  access?: Role;
  owner?: Person | null;
  general?: GeneralAccess;
  shared?: boolean;
  /** Access requests waiting for the owner (counted for the owner only). */
  requests?: number;
  /** The group it is in (exuvia/groups.py). */
  group?: GroupRef | null;
  /** When the caller last opened it and last changed it (creating it counts), and when anyone last changed it (exuvia/activity.py); null: not known. */
  opened_at?: number | null;
  edited_at?: number | null;
  changed_at?: number | null;
}

/** `GET /api/projects`. `sharing` is null on a hub nobody has to get in to. */
export interface HubListing {
  projects: HubProject[];
  templates: string[];
  auth: boolean;
  sharing: { people: boolean; default_access: GeneralAccess } | null;
  /** The groups the caller has a role on. */
  groups?: HubGroup[];
  /** The hub's own variables, which every project gets: how many, and whether the caller (an admin) can change them. */
  hub_env?: { variables: number; can_edit: boolean };
}

/** A group of hub projects (exuvia/groups.py), as `GET /api/groups` lists it. */
export interface HubGroup {
  id: string;
  name: string;
  owner: Person | null;
  /** The caller's role on it. */
  access: Role;
  projects: number;
  variables: number;
}

/** `GET /api/groups/<id>`: everything about one group the caller may see. */
export interface Group {
  id: string;
  name: string;
  owner: Person | null;
  you: Role;
  /** Sets variables, adds and removes projects. */
  can_edit: boolean;
  /** Manages people, renames, hands over, deletes. */
  can_manage: boolean;
  people_enabled: boolean;
  people: Member[];
  projects: { id: string; name: string; access: Role | null }[];
  variables: EnvVar[];
}

/** `GET <project>/api/access`: what this page may do in its project. */
export interface Access {
  role: Role | null;
  /** The hub shares projects (else everyone who gets in can do everything). */
  sharing: boolean;
  /** People can be named (the hub signs people in); else it shares by link only. */
  people: boolean;
  user: Me["user"];
  /** A share link let this page in. */
  link?: boolean;
  owner?: Person | null;
  general?: GeneralAccess;
  can_request?: boolean;
  requested?: { role: Role; message: string; created_at: number } | null;
  /** The group the project is in, and the caller's role on it (null: none). */
  group?: (GroupRef & { access: Role | null }) | null;
}

/** One person a project is shared with by name. */
export interface Member {
  principal: string;
  kind: "user" | "email";
  role: Role;
  user: Person;
  /** Named by an email nobody has signed in with yet. */
  pending: boolean;
  created_at: number;
}

/** A read-only share link: never the token, except in the answer that creates it. */
export interface ShareLink {
  id: string;
  name: string;
  hint: string;
  role: "viewer";
  created_at: number;
  expires_at: number | null;
  last_used: number | null;
  expired?: boolean;
}

export interface AccessRequest {
  user_id: string;
  user: Person;
  role: Role;
  message: string;
  created_at: number;
}

/** `GET /api/projects/<id>/sharing`. `links` and `requests` come to whoever can manage it. */
export interface Sharing {
  project: string;
  you: Role;
  can_manage: boolean;
  people_enabled: boolean;
  default_access: GeneralAccess;
  owner: Person | null;
  general: GeneralAccess;
  /** The project has no settings of its own: it follows the hub's default. */
  default: boolean;
  people: Member[];
  links?: ShareLink[];
  requests?: AccessRequest[];
}

/** The page's own project's access (not on `exuvia serve`, whose answer is always full access). */
export const access = async () => (await sendTo("GET", apiUrl("/api/access"))).json() as Promise<Access>;

const sharingUrl = (project: string, rest = "") => `/api/projects/${encodeURIComponent(project)}/sharing${rest}`;
const json = async <T,>(r: Promise<Response>) => (await r).json() as Promise<T>;

/** A hub project's sharing (exuvia/server.py `_sharing_routes`). */
export const sharingApi = {
  get: (p: string) => json<Sharing>(sendTo("GET", sharingUrl(p))),
  patch: (p: string, change: { general?: GeneralAccess; owner?: string }) => json<Sharing>(sendTo("PATCH", sharingUrl(p), change)),
  share: (p: string, who: { user?: string; email?: string; role: Role }) => json<Sharing>(sendTo("PUT", sharingUrl(p, "/people"), who)),
  /** Take someone off; the answer is `{left: true}` when you took yourself off and can't open it any more. */
  unshare: (p: string, principal: string) =>
    json<Sharing | { ok: true; left: true }>(sendTo("DELETE", sharingUrl(p, `/people/${encodeURIComponent(principal)}`))),
  createLink: (p: string, b: { name?: string; expires_in_days?: number | null }) =>
    json<ShareLink & { token: string; url: string }>(sendTo("POST", sharingUrl(p, "/links"), b)),
  revokeLink: (p: string, id: string) => json<Sharing>(sendTo("DELETE", sharingUrl(p, `/links/${encodeURIComponent(id)}`))),
  request: (p: string, b: { role: Role; message?: string }) => json<{ ok: true }>(sendTo("POST", sharingUrl(p, "/requests"), b)),
  approve: (p: string, uid: string, role?: Role) =>
    json<Sharing>(sendTo("POST", sharingUrl(p, `/requests/${encodeURIComponent(uid)}/approve`), { role })),
  dismiss: (p: string, uid: string) => json<Sharing>(sendTo("DELETE", sharingUrl(p, `/requests/${encodeURIComponent(uid)}`))),
  /** People to share with: enabled users whose name or email contains `q`. */
  people: async (q: string) =>
    (await json<{ people: Person[] }>(sendTo("GET", `/api/people?q=${encodeURIComponent(q)}`))).people,
};

const groupUrl = (gid: string, rest = "") => `/api/groups/${encodeURIComponent(gid)}${rest}`;
const envOf = (g: Group): EnvState => ({ variables: g.variables, requests: [] });

/** A hub's groups (exuvia/server.py `_group_routes`). */
export const groupApi = {
  list: () => json<{ groups: HubGroup[]; can_create: boolean; people: boolean }>(sendTo("GET", "/api/groups")),
  create: (name: string) => json<Group>(sendTo("POST", "/api/groups", { name })),
  get: (gid: string) => json<Group>(sendTo("GET", groupUrl(gid))),
  patch: (gid: string, change: { name?: string; owner?: string }) => json<Group>(sendTo("PATCH", groupUrl(gid), change)),
  remove: (gid: string) => json<{ ok: true; projects: string[] }>(sendTo("DELETE", groupUrl(gid))),
  share: (gid: string, who: { user?: string; email?: string; role: Role }) => json<Group>(sendTo("PUT", groupUrl(gid, "/people"), who)),
  /** Take someone off; `{left: true}` when you took yourself off. */
  unshare: (gid: string, principal: string) =>
    json<Group | { ok: true; left: true }>(sendTo("DELETE", groupUrl(gid, `/people/${encodeURIComponent(principal)}`))),
  setEnv: async (gid: string, name: string, b: { value?: string; secret: boolean }) =>
    json<EnvState>(sendTo("PUT", groupUrl(gid, `/env/${encodeURIComponent(name)}`), b)),
  removeEnv: async (gid: string, name: string) => json<EnvState>(sendTo("DELETE", groupUrl(gid, `/env/${encodeURIComponent(name)}`))),
  env: async (gid: string) => envOf(await groupApi.get(gid)),
  /** Put a project in a group (out of any other), or out of its group with `null`. */
  setProjectGroup: (project: string, group: string | null) =>
    json<{ project: string; group: GroupRef | null }>(sendTo("PUT", `/api/projects/${encodeURIComponent(project)}/group`, { group })),
};

/** The hub's own variables (`/api/hub/env`): everyone on the hub reads them, admins change them. */
export const hubEnvApi = {
  env: () => json<EnvState & { can_edit: boolean }>(sendTo("GET", "/api/hub/env")),
  setEnv: (name: string, b: { value?: string; secret: boolean }) =>
    json<EnvState>(sendTo("PUT", `/api/hub/env/${encodeURIComponent(name)}`, b)),
  removeEnv: (name: string) => json<EnvState>(sendTo("DELETE", `/api/hub/env/${encodeURIComponent(name)}`)),
};

/** Project ids a hub accepts (exuvia/hub.py PROJECT_ID). */
export const PROJECT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

/** The hub's own API (`/api/projects`), for the hub's home page at `/`. */
export const hubApi = {
  /** Every project and template; `token` checks a token before the browser keeps it. */
  projects: async (token?: string) =>
    (await sendTo("GET", "/api/projects", undefined, undefined, token)).json() as Promise<HubListing>,
  /** `group`: a group the caller edits, to create the project in. */
  create: async (b: { id: string; name?: string; template?: string; access?: GeneralAccess; group?: string }) =>
    (await sendTo("POST", "/api/projects", b)).json() as Promise<{ id: string; name: string; group?: GroupRef | null }>,
  remove: async (id: string) => {
    await sendTo("DELETE", `/api/projects/${encodeURIComponent(id)}`);
  },
};

/** A saved conversation, as the list of past chats shows it. */
export interface AgentChat {
  id: string;
  /** The first thing asked, shortened. */
  title: string;
  created_at: number;
  updated_at: number;
  started_by: string | null;
  running: boolean;
}

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
