/**
 * `import { useTable, useQuery, useParams } from "exuvia"` — the API a view's
 * code uses, inside its iframe. Every call is a message to the host page
 * (see protocol.ts); the host answers only for nodes in the view's `reads`.
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { tableFromIPC, type Table } from "apache-arrow";
import type { DownloadFormat, Method, ParamSpec, Params, ParamsState, RequestArgs, ResultOf, ViewBundle } from "./protocol";

export type { Table };

type Call = <M extends Method>(method: M, args: RequestArgs[M]) => Promise<ResultOf[M]>;

interface State {
  view: { name: string; title: string; reads: string[] };
  params: ParamsState;
  dark: boolean;
  /** Per read node, bumped when its output is replaced. */
  versions: Record<string, number>;
}

let call: Call = () => Promise.reject(new Error("The view runtime is not connected."));
let state: State = { view: { name: "", title: "", reads: [] }, params: { params: {}, specs: [] }, dark: false, versions: {} };
const listeners = new Set<() => void>();
const changeListeners = new Set<(nodes: string[]) => void>();

function set(next: Partial<State>): void {
  state = { ...state, ...next };
  listeners.forEach((l) => l());
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** Wiring for runtime.tsx: not part of the API views use. */
export const internal = {
  connect(fn: Call, view: ViewBundle, params: ParamsState, dark: boolean) {
    call = fn;
    set({ view: { name: view.name, title: view.title, reads: view.reads }, params, dark });
  },
  changed(nodes: string[], params: ParamsState) {
    const versions = { ...state.versions };
    nodes.forEach((n) => (versions[n] = (versions[n] ?? 0) + 1));
    set({ versions, params });
    changeListeners.forEach((l) => l(nodes));
  },
  theme(dark: boolean) {
    set({ dark });
  },
};

// -- plain calls ------------------------------------------------------------------

/** The node an output name belongs to: `fit` for `fit.coefs` (one output of a node with several). */
const nodeOf = (name: string) => name.split(".")[0];

/** The current output of a node the view reads, as an Arrow table
 * (`table("fit.coefs")` for one output of a node with several). */
export async function table(node: string): Promise<Table> {
  return tableFromIPC(new Uint8Array(await call("table", { node })));
}

/** The current output of a node whose output is JSON (`json("fit.metrics")` for one of several). */
export async function json<T = unknown>(node: string): Promise<T> {
  return (await call("json", { node })) as T;
}

/** DuckDB SQL over the outputs of the nodes the view reads (each is a table
 * named after its node; `<node>__runs` has every parameter set it ran with; a node
 * with several outputs is a schema: `<node>.<part>` and `<node>.<part>__runs`). */
export async function query(sql: string): Promise<Table> {
  return tableFromIPC(new Uint8Array(await call("query", { sql })));
}

/** Save data as a file: a read node's whole output (`download("prices", "xlsx")`,
 * `"fit.coefs"` for one output of a node with several) or a query's whole result
 * (`download({ sql: "select …" }, "csv")`), for the parameters the view shows.
 * `format` is csv (the default), xlsx, parquet or json; `filename` leaves out the extension. */
export async function download(source: string | { sql: string }, format: DownloadFormat = "csv", filename?: string): Promise<void> {
  const what = typeof source === "string" ? { node: source } : { sql: source.sql };
  await call("download", { ...what, format, ...(filename ? { filename } : {}) });
}

/** The run parameters the view is showing, and the declared ones. */
export function getParams(): ParamsState {
  return state.params;
}

/** Show the outputs for other parameter values (`{as_of: "2026-03-02"}`).
 * This never runs anything: outputs that do not exist for those values
 * are reported as missing. */
export function setParams(params: Params): Promise<ParamsState> {
  return call("setParams", { params });
}

/** Called with the nodes whose output was replaced (a run finished, or the
 * parameters changed). Returns an unsubscribe function. */
export function onChange(fn: (nodes: string[]) => void): () => void {
  changeListeners.add(fn);
  return () => changeListeners.delete(fn);
}

/** Arrow rows as plain objects; 64-bit integers become numbers when they fit. */
export function rows<T = Record<string, unknown>>(t: Table | null | undefined): T[] {
  if (!t) return [];
  const out: T[] = [];
  for (const row of t) {
    const o = (row as unknown as { toJSON(): Record<string, unknown> }).toJSON();
    for (const k in o) {
      const v = o[k];
      if (typeof v === "bigint" && v <= BigInt(Number.MAX_SAFE_INTEGER) && v >= BigInt(Number.MIN_SAFE_INTEGER)) o[k] = Number(v);
    }
    out.push(o as T);
  }
  return out;
}

// -- hooks ------------------------------------------------------------------------

export interface Loaded<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
}

/** Load `fetcher()` whenever `key` changes; keep the last data while reloading. */
function useLoad<T>(key: string, fetcher: () => Promise<T>): Loaded<T> {
  const [out, setOut] = useState<Loaded<T>>({ data: undefined, error: null, loading: true });
  const latest = useRef(fetcher);
  latest.current = fetcher;
  useEffect(() => {
    let live = true;
    setOut((o) => ({ ...o, loading: true }));
    latest.current().then(
      (data) => live && setOut({ data, error: null, loading: false }),
      (e: unknown) => live && setOut((o) => ({ data: o.data, error: e instanceof Error ? e.message : String(e), loading: false })),
    );
    return () => {
      live = false;
    };
  }, [key]);
  return out;
}

function useStore(): State {
  return useSyncExternalStore(subscribe, () => state);
}

/** A node's current table (`"fit.coefs"`: one output of a node with several); reloads when the node's output is replaced. */
export function useTable(node: string): Loaded<Table> {
  const s = useStore();
  return useLoad(`${node}|${s.versions[nodeOf(node)] ?? 0}`, () => table(node));
}

/** A node's current JSON output (`"fit.metrics"`: one of several); reloads when it is replaced. */
export function useJson<T = unknown>(node: string): Loaded<T> {
  const s = useStore();
  return useLoad(`${node}|${s.versions[nodeOf(node)] ?? 0}`, () => json<T>(node));
}

/** A query over the view's reads; reloads when the SQL or any read output changes. */
export function useQuery(sql: string): Loaded<Table> {
  const s = useStore();
  const v = s.view.reads.map((n) => s.versions[n] ?? 0).join(",");
  return useLoad(`${sql}|${v}`, () => query(sql));
}

/** `[values, setParams, declared]` for the run parameters the view shows. */
export function useParams(): [Params, (p: Params) => Promise<ParamsState>, ParamSpec[]] {
  const s = useStore();
  const update = useCallback((p: Params) => setParams(p), []);
  return [s.params.params, update, s.params.specs];
}

/** The view's name, title and reads. */
export function useView(): State["view"] {
  return useStore().view;
}

/** True when the exuvia page is in dark mode (the CSS variables follow it too). */
export function useDark(): boolean {
  return useStore().dark;
}
