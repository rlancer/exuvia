/**
 * The host page's side of a view: builds the sandboxed iframe document and
 * answers its requests (protocol.ts). The host is the gatekeeper: it answers
 * `table`/`json` only for nodes in the view's `reads`, and sends every data
 * request to the view-scoped routes (`/api/views/{name}/data/{node}`,
 * `/api/views/{name}/query`), which refuse other nodes on the server too.
 */
import { api, apiUrl, type ProjectView } from "../api";
import { saveQuery, saveUrl, viewExportUrl } from "../download";
import {
  CHANNEL,
  isToHost,
  type ErrorPhase,
  type Method,
  type Params,
  type ParamsState,
  type RequestArgs,
  type ResultOf,
  type ToFrameBody,
  type ToHost,
} from "./protocol";

declare const __EXUVIA_BUILD__: string;

/** The theme variables a view's CSS can use (`var(--text)`), light and dark. */
const BASE_CSS = `
:root{--bg:#f6f5f1;--panel:#fff;--panel-2:#f0eee8;--line:#e2dfd6;--text:#1b1c1f;--muted:#6c6a63;--faint:#9a978d;
--accent:#3b6fd8;--fresh:#2f8f5b;--stale:#c98a12;--failed:#c4413a;--running:#3b6fd8;
--mono:"JetBrains Mono","Cascadia Code",ui-monospace,SFMono-Regular,Consolas,monospace;
--sans:"Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color-scheme:light}
:root[data-theme=dark]{--bg:#111214;--panel:#17181b;--panel-2:#1d1f23;--line:#2a2c31;--text:#e9e7e1;--muted:#9b998f;
--faint:#6d6b64;--accent:#6f9bff;--fresh:#4cc38a;--stale:#f2b84b;--failed:#ef6b62;--running:#6f9bff;color-scheme:dark}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--text);font:13px/1.45 var(--sans);-webkit-font-smoothing:antialiased}
.exv-error{margin:16px;padding:10px 12px;border:1px solid var(--failed);border-radius:6px;color:var(--text)}
.exv-error pre{margin:6px 0 0;font:12px/1.45 var(--mono);white-space:pre-wrap}`;

/** Where the page fetches the runtime script (built into exuvia/static/view-runtime/, served under the API). */
export function runtimeUrl(): string {
  const build = typeof __EXUVIA_BUILD__ === "string" ? __EXUVIA_BUILD__ : "dev";
  return apiUrl(`/api/view-runtime/runtime.js?b=${build}`);
}

let runtimeSource: Promise<string> | null = null;

/** The runtime's source, fetched once per page (same origin, so the API token cookie rides along if set). */
function loadRuntime(): Promise<string> {
  runtimeSource ??= fetch(runtimeUrl()).then((r) => {
    if (!r.ok) throw new Error(`views runtime: ${r.status} ${r.statusText} (run \`npm run build\` in ui/)`);
    return r.text();
  });
  runtimeSource.catch(() => (runtimeSource = null));
  return runtimeSource;
}

/** Runs in the frame: wait for the runtime's source from the page and evaluate it. */
const BOOTSTRAP = `(function(){var C=${JSON.stringify(CHANNEL)};
function boot(e){var d=e.data;if(e.source!==parent||!d||d.channel!==C||d.type!=="runtime")return;
removeEventListener("message",boot);(0,eval)(d.source+"\\n//# sourceURL=exuvia-view-runtime.js");}
addEventListener("message",boot);parent.postMessage({channel:C,type:"boot"},"*");})();`;

function nonce(): string {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return btoa(String.fromCharCode(...b));
}

/**
 * The iframe document: theme variables and a bootstrap script, nothing else.
 * The frame fetches nothing (a sandboxed frame's opaque origin would not be
 * allowed to reach a local server anyway): its CSP names no URL at all, so
 * a view cannot reach the API, the internet or anything else, and lets only
 * the bootstrap (by nonce) and code it evaluates (the runtime, then the
 * view's compiled files) run.
 */
export function frameDocument(n = nonce()): string {
  const csp = [
    "default-src 'none'",
    `script-src 'nonce-${n}' 'unsafe-eval'`,
    "style-src 'unsafe-inline'",
    "img-src data: blob:",
    "font-src data:",
    "connect-src 'none'",
    "form-action 'none'",
    "base-uri 'none'",
  ].join("; ");
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>${BASE_CSS}</style></head><body><div id="root"></div>
<script nonce="${n}">${BOOTSTRAP}</script></body></html>`;
}

export type FrameStatus =
  | { phase: "loading" | "compiled" | "rendered" }
  | { phase: ErrorPhase; error: string; file?: string; stack?: string };

export interface BridgeOptions {
  /** Change the parameter set the view shows; resolves with the new state. */
  setParams: (params: Params) => Promise<ParamsState>;
  onStatus: (s: FrameStatus) => void;
}

/** `fit.coefs` names the `coefs` output of a node `fit` that returns several; `fit` names the node's only output. */
export function splitOutput(name: string): { node: string; part: string | null } {
  const dot = name.indexOf(".");
  return dot < 0 ? { node: name, part: null } : { node: name.slice(0, dot), part: name.slice(dot + 1) };
}

/** Why the host refuses a node (or `node.part`), or null when the view may read it. */
export function refusal(view: Pick<ProjectView, "name" | "reads">, name: string): string | null {
  const { node } = splitOutput(name);
  return view.reads.includes(node) ? null : `View ${view.name} does not read ${node}: add it to "reads" in view.json.`;
}

/** What a `table`/`json` request for `name` reads, or why it cannot. */
export function resolveOutput(
  outputs: ProjectView["outputs"],
  name: string,
  method: "table" | "json",
): { node: string; part: string | null; artifact_id: string } {
  const { node, part } = splitOutput(name);
  const other = method === "table" ? "json" : "table";
  const out = outputs.find((o) => o.node === node);
  if (!out?.artifact_id) throw new Error(`${node} has no output yet for these parameters (views never run nodes).`);
  if (out.kind === "multi") {
    const parts = out.parts ?? [];
    const names = parts.map((p) => `${node}.${p.name}`).join(", ");
    if (part === null) throw new Error(`${node} has several outputs: read one of ${names}.`);
    const found = parts.find((p) => p.name === part);
    if (!found) throw new Error(`${node} has no output ${part}: it has ${names}.`);
    if (found.kind !== method) throw new Error(`${name} is ${found.kind}, not ${method === "table" ? "a table" : "JSON"}: use ${other}().`);
    return { node, part, artifact_id: out.artifact_id };
  }
  if (part !== null) throw new Error(`${node} has a single output: read it as ${node}.`);
  if (method === "table" && out.kind !== "table") throw new Error(`${node}'s output is ${out.kind}, not a table: use json().`);
  if (method === "json" && out.kind !== "json") throw new Error(`${node}'s output is ${out.kind}, not JSON: use table().`);
  return { node, part, artifact_id: out.artifact_id };
}

export class ViewBridge {
  private view: ProjectView;
  private params: ParamsState;
  private dark: boolean;
  private cache = new Map<string, Promise<ResultOf["table"] | ResultOf["json"]>>();

  constructor(
    private frame: HTMLIFrameElement,
    view: ProjectView,
    params: ParamsState,
    dark: boolean,
    private opts: BridgeOptions,
  ) {
    this.view = view;
    this.params = params;
    this.dark = dark;
  }

  /** Start answering the frame; returns the function that stops it. */
  start(): () => void {
    const on = (e: MessageEvent) => {
      if (e.source !== this.frame.contentWindow || !isToHost(e.data)) return;
      void this.handle(e.data);
    };
    window.addEventListener("message", on);
    return () => window.removeEventListener("message", on);
  }

  private post(msg: ToFrameBody, transfer: Transferable[] = []): void {
    this.frame.contentWindow?.postMessage({ channel: CHANNEL, ...msg }, "*", transfer);
  }

  /** New view info or parameter set: tell the frame which outputs changed. */
  update(view: ProjectView, params: ParamsState): void {
    const before = new Map(this.view.outputs.map((o) => [o.node, o.artifact_id]));
    const changed = view.outputs.filter((o) => before.get(o.node) !== o.artifact_id).map((o) => o.node);
    const paramsChanged = JSON.stringify(params.params) !== JSON.stringify(this.params.params);
    this.view = view;
    this.params = params;
    if (changed.length || paramsChanged) this.post({ type: "changed", nodes: changed, params });
  }

  setTheme(dark: boolean): void {
    if (dark === this.dark) return;
    this.dark = dark;
    this.post({ type: "theme", dark });
  }

  private async handle(msg: ToHost): Promise<void> {
    if (msg.type === "boot") {
      this.opts.onStatus({ phase: "loading" });
      try {
        this.post({ type: "runtime", source: await loadRuntime() });
      } catch (e) {
        this.opts.onStatus({ phase: "compile", error: (e as Error).message });
      }
      return;
    }
    if (msg.type === "ready") {
      this.opts.onStatus({ phase: "loading" });
      try {
        const got = await api.view(this.view.name);
        const files: Record<string, string> = {};
        got.sources.forEach((f) => (files[f.path] = f.source));
        this.post({
          type: "load",
          view: { name: got.name, title: got.title, entry: got.entry, reads: got.reads, files },
          params: this.params,
          dark: this.dark,
        });
      } catch (e) {
        this.opts.onStatus({ phase: "compile", error: `Could not load the view: ${(e as Error).message}` });
      }
      return;
    }
    if (msg.type === "status") {
      this.opts.onStatus("error" in msg ? { phase: msg.phase, error: msg.error, file: msg.file, stack: msg.stack } : { phase: msg.phase });
      return;
    }
    try {
      const result = await this.answer(msg.method, msg.args);
      if (result instanceof ArrayBuffer) {
        const copy = result.slice(0); // the cached buffer stays usable
        this.post({ type: "response", id: msg.id, ok: true, result: copy }, [copy]);
      } else this.post({ type: "response", id: msg.id, ok: true, result });
    } catch (e) {
      this.post({ type: "response", id: msg.id, ok: false, error: e instanceof Error ? e.message : String(e) });
    }
  }

  private async answer<M extends Method>(method: M, args: RequestArgs[M]): Promise<ResultOf[Method]> {
    const name = this.view.name;
    switch (method) {
      case "params":
        return this.params;
      case "setParams":
        return this.opts.setParams((args as RequestArgs["setParams"]).params);
      case "query":
        return api.viewQuery(name, (args as RequestArgs["query"]).sql, this.params.params);
      case "download": {
        const a = args as RequestArgs["download"];
        // The frame cannot save files (its sandbox has no allow-downloads): the page does.
        if (a.sql !== undefined) {
          await saveQuery(a.sql, a.format, { view: name, params: this.params.params, filename: a.filename });
          return null;
        }
        const asked = a.node ?? "";
        const refused = refusal(this.view, asked);
        if (refused) throw new Error(refused);
        // Say what is wrong here: a link's failure never reaches the view.
        try {
          resolveOutput(this.view.outputs, asked, "table");
        } catch (e) {
          if (a.format !== "json") throw e;
          resolveOutput(this.view.outputs, asked, "json");
        }
        const { node, part } = splitOutput(asked);
        saveUrl(viewExportUrl(name, node, a.format, { part, params: this.params.params, filename: a.filename }));
        return null;
      }
      case "table":
      case "json": {
        const asked = (args as RequestArgs["table"]).node;
        const refused = refusal(this.view, asked);
        if (refused) throw new Error(refused);
        const { node, part, artifact_id } = resolveOutput(this.view.outputs, asked, method);
        // An artifact id names its content (every part of it), so an answer for it never goes stale.
        const key = `${asked}@${artifact_id}`;
        let hit = this.cache.get(key);
        if (!hit) {
          hit = api.viewData(name, node, this.params.params, part ?? undefined).then((d) => {
            if (d.kind === "table") return d.arrow;
            if (d.kind === "json") return d.value;
            throw new Error(`${asked} has no output yet for these parameters.`);
          });
          hit.catch(() => this.cache.delete(key));
          this.cache.set(key, hit);
        }
        return hit;
      }
    }
    throw new Error(`unknown method ${method as string}`);
  }
}
