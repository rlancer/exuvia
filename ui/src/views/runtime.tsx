/**
 * The views runtime: the only script a view's sandboxed iframe loads. It
 * waits for the host page to send the view's files, compiles them in the
 * browser (loader.ts) and renders the entry file's default export. React,
 * Arrow, Vega-Embed and the TSX compiler are bundled here (vite.runtime.config.ts),
 * so nothing is fetched from the internet.
 */
import * as React from "react";
import * as jsxRuntime from "react/jsx-runtime";
import * as ReactDOM from "react-dom";
import * as ReactDOMClient from "react-dom/client";
import * as Arrow from "apache-arrow";
import vegaEmbed, * as VegaEmbed from "vega-embed";
import { CHANNEL, isToFrame, type ErrorPhase, type Method, type RequestArgs, type ResultOf, type ToFrame, type ToHostBody } from "./protocol";
import { ViewError, createLoader } from "./loader";
import * as sdk from "./sdk";

/** A module object an `import x from` / `import * as x from` both work with. */
function esm(ns: object, def: unknown = ns): Record<string, unknown> {
  return { ...(ns as Record<string, unknown>), __esModule: true, default: def };
}

const { internal, ...sdkApi } = sdk;
void internal;

/** Every package a view may import by bare name. */
export const BUILTINS: Record<string, unknown> = {
  react: esm(React, React),
  "react/jsx-runtime": esm(jsxRuntime),
  "react-dom": esm(ReactDOM, ReactDOM),
  "react-dom/client": esm(ReactDOMClient, ReactDOMClient),
  "apache-arrow": esm(Arrow),
  "vega-embed": esm(VegaEmbed, vegaEmbed),
  exuvia: esm(sdkApi),
};

function post(msg: ToHostBody): void {
  window.parent.postMessage({ channel: CHANNEL, ...msg }, "*");
}

function report(phase: ErrorPhase, e: unknown): void {
  const err = e instanceof Error ? e : new Error(String(e));
  post({ type: "status", phase, error: err.message, file: err instanceof ViewError ? err.file : undefined, stack: err.stack });
}

// -- requests to the host ---------------------------------------------------------
let seq = 0;
const pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();

function request<M extends Method>(method: M, args: RequestArgs[M]): Promise<ResultOf[M]> {
  const id = ++seq;
  return new Promise<ResultOf[M]>((resolve, reject) => {
    pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
    window.parent.postMessage({ channel: CHANNEL, type: "request", id, method, args }, "*");
  });
}

// -- rendering --------------------------------------------------------------------
class Boundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    report("runtime", Object.assign(new Error(error.message), { stack: `${error.stack ?? ""}\n${info.componentStack ?? ""}` }));
  }
  render() {
    if (this.state.error) {
      return (
        <div className="exv-error">
          <b>This view crashed.</b>
          <pre>{this.state.error.message}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}

function Rendered({ children }: { children: React.ReactNode }) {
  React.useEffect(() => post({ type: "status", phase: "rendered" }), []);
  return <>{children}</>;
}

function setTheme(dark: boolean): void {
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
}

function load(msg: Extract<ToFrame, { type: "load" }>): void {
  setTheme(msg.dark);
  document.title = msg.view.title || msg.view.name;
  sdk.internal.connect(request, msg.view, msg.params, msg.dark, msg.refreshing ?? []);
  const require = createLoader({
    files: msg.view.files,
    builtins: BUILTINS,
    packages: msg.view.packages,
    onCss: (path, css) => {
      const style = document.createElement("style");
      style.dataset.file = path;
      style.textContent = css;
      document.head.appendChild(style);
    },
  });
  let Entry: unknown;
  try {
    Entry = require(msg.view.entry).default;
    if (typeof Entry !== "function" && !(typeof Entry === "object" && Entry !== null && "$$typeof" in Entry)) {
      throw new ViewError(`${msg.view.entry} must \`export default\` a React component.`, msg.view.entry);
    }
  } catch (e) {
    report("compile", e);
    return;
  }
  post({ type: "status", phase: "compiled" });
  const Component = Entry as React.ComponentType;
  ReactDOMClient.createRoot(document.getElementById("root")!).render(
    <Boundary>
      <Rendered>
        <Component />
      </Rendered>
    </Boundary>,
  );
}

let loaded = false;
window.addEventListener("message", (e: MessageEvent) => {
  if (e.source !== window.parent || !isToFrame(e.data)) return;
  const msg = e.data;
  switch (msg.type) {
    case "load":
      if (!loaded) {
        loaded = true;
        load(msg);
      }
      break;
    case "response": {
      const p = pending.get(msg.id);
      if (!p) return;
      pending.delete(msg.id);
      if (msg.ok) p.resolve(msg.result);
      else p.reject(new Error(msg.error));
      break;
    }
    case "changed":
      sdk.internal.changed(msg.nodes, msg.params);
      break;
    case "refreshing":
      sdk.internal.refreshing(msg.nodes);
      break;
    case "theme":
      setTheme(msg.dark);
      sdk.internal.theme(msg.dark);
      break;
  }
});

window.addEventListener("error", (e) => report("runtime", e.error ?? e.message));
window.addEventListener("unhandledrejection", (e) => report("runtime", e.reason));

post({ type: "ready" });
