import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type GraphNode, type Project, type ProjectParam, type ProjectView } from "./api";
import { ViewBridge, frameDocument, type FrameStatus } from "./views/host";
import type { Params, ParamsState } from "./views/protocol";
import { IconPlay } from "./icons";

/** Overrides → the parameter set a view sees (resolved values + declarations). */
function paramsState(params: ProjectParam[]): ParamsState {
  return { params: Object.fromEntries(params.map((p) => [p.name, p.value])), specs: params };
}

const VIEW_STATE_LABEL: Record<ProjectView["state"], string> = {
  ready: "ready",
  missing: "missing",
  stale: "stale",
  error: "error",
};
// View states mapped onto the node state colours.
const VIEW_STATE_CLASS: Record<ProjectView["state"], string> = {
  ready: "s-fresh",
  missing: "s-new",
  stale: "s-stale",
  error: "s-failed",
};

/**
 * Views mode: the project's views (custom UI under views/) next to the
 * canvas. A view renders in a sandboxed iframe with the parameter set chosen
 * here; it only reads outputs and never runs anything.
 */
export function Views(props: {
  project: Project | null;
  nodes: GraphNode[];
  dark: boolean;
  onOpenNode: (name: string) => void;
}) {
  const { project, dark, onOpenNode } = props;
  const [overrides, setOverrides] = useState<Params>({});
  const [at, setAt] = useState<Project | null>(null);
  const [paramErr, setParamErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  // `project` changes on every graph/run event: refetch at our parameter set.
  useEffect(() => {
    let live = true;
    api.project(overrides).then(
      (p) => live && (setAt(p), setParamErr(null)),
      (e: Error) => live && setParamErr(e.message),
    );
    return () => {
      live = false;
    };
  }, [project, overrides]);

  const views = at?.views ?? [];
  const view = views.find((v) => v.name === selected) ?? views[0] ?? null;
  const state = useMemo(() => paramsState(at?.params ?? []), [at]);

  /** Change parameters (from the bar or from a view); throws on a bad value. */
  const changeParams = useCallback(
    async (patch: Params): Promise<ParamsState> => {
      const next = { ...overrides, ...patch };
      for (const k of Object.keys(next)) if (next[k] === null || next[k] === "") delete next[k];
      const p = await api.project(next); // validates: unknown or bad values are a 400
      setOverrides(next);
      setAt(p);
      setParamErr(null);
      return paramsState(p.params);
    },
    [overrides],
  );

  return (
    <div className="views">
      <aside className="views-list">
        <h4>Views</h4>
        {views.map((v) => (
          <button
            key={v.name}
            aria-current={view?.name === v.name}
            className={`views-item ${VIEW_STATE_CLASS[v.state]} ${view?.name === v.name ? "on" : ""}`}
            onClick={() => setSelected(v.name)}
            title={v.reason ?? v.description}
          >
            <span className="row1">
              <span className="dot" />
              <span className="name">{v.title || v.name}</span>
              <span className="state">{VIEW_STATE_LABEL[v.state]}</span>
            </span>
            {v.reason ? <span className="reason">{v.reason}</span> : v.description ? <span className="reason">{v.description}</span> : null}
          </button>
        ))}
        {at && views.length === 0 && (
          <div className="views-empty">
            No views yet. A view is a folder <code>views/&lt;name&gt;/</code> with a <code>view.json</code> (
            <code>{'{"title", "reads": [nodes]}'}</code>) and an <code>App.tsx</code> whose default export is a React
            component. Ask the agent to write one.
          </div>
        )}
      </aside>
      <section className="views-main">
        {view ? (
          <ViewPane
            key={view.name}
            view={view}
            nodes={props.nodes}
            params={state}
            specs={at?.params ?? []}
            overrides={overrides}
            paramErr={paramErr}
            dark={dark}
            onParams={changeParams}
            onOpenNode={onOpenNode}
          />
        ) : (
          <div className="placeholder">{at ? "Pick a view." : "Loading…"}</div>
        )}
      </section>
    </div>
  );
}

function ViewPane(props: {
  view: ProjectView;
  nodes: GraphNode[];
  params: ParamsState;
  specs: ProjectParam[];
  overrides: Params;
  paramErr: string | null;
  dark: boolean;
  onParams: (p: Params) => Promise<ParamsState>;
  onOpenNode: (name: string) => void;
}) {
  const { view, params, dark } = props;
  const [status, setStatus] = useState<FrameStatus>({ phase: "loading" });
  const [errors, setErrors] = useState<Extract<FrameStatus, { error: string }>[]>([]);
  const [runErr, setRunErr] = useState<string | null>(null);
  const known = new Set(props.nodes.map((n) => n.name));
  const outputs = new Map(view.outputs.map((o) => [o.node, o]));
  const toRun = view.outputs.filter((o) => o.state !== "fresh" || !o.artifact_id).map((o) => o.node);

  const onStatus = useCallback((s: FrameStatus) => {
    setStatus(s);
    if ("error" in s) setErrors((es) => [...es.slice(-19), s]);
  }, []);
  // A new version of the view's files starts over with a fresh frame.
  useEffect(() => {
    setErrors([]);
    setStatus({ phase: "loading" });
  }, [view.hash]);

  const runReads = async () => {
    setRunErr(null);
    try {
      await api.runWith(toRun, props.overrides);
    } catch (e) {
      setRunErr((e as Error).message);
    }
  };

  return (
    <div className="view-pane">
      <div className="view-head">
        <div className="insp-title">
          <h3>{view.title || view.name}</h3>
          <span className={`view-state ${VIEW_STATE_CLASS[view.state]}`}>
            <span className="dot" /> {view.state}
          </span>
          <span className="phase">{"error" in status ? `${status.phase} error` : status.phase}</span>
        </div>
        {view.description && <div className="insp-sub">{view.description}</div>}
        <div className="view-reads" title="The nodes this view reads (view.json reads)">
          <span className="lbl">reads</span>
          {view.reads.map((n) => {
            const o = outputs.get(n);
            return (
              <button
                key={n}
                className={`read-chip ${o ? `s-${o.state}` : "s-error"}`}
                onClick={() => known.has(n) && props.onOpenNode(n)}
                disabled={!known.has(n)}
                title={!known.has(n) ? "no such node" : o?.artifact_id ? `${o.state} · ${o.kind}` : "no output for these parameters"}
              >
                <span className="dot" /> {n}
                {o && !o.artifact_id && <span className="miss">no output</span>}
              </button>
            );
          })}
          {toRun.length > 0 && (
            <button className="btn sm" onClick={runReads} title="Run the read nodes that are missing or stale, for these parameters">
              <IconPlay size={10} /> Run {toRun.length === 1 ? toRun[0] : `${toRun.length} nodes`}
            </button>
          )}
        </div>
        {props.specs.length > 0 && <ParamBar specs={props.specs} overrides={props.overrides} onChange={props.onParams} />}
        {(props.paramErr || runErr) && <div className="err-banner"><div>{props.paramErr ?? runErr}</div></div>}
        {view.state !== "ready" && view.reason && <div className={`view-reason ${VIEW_STATE_CLASS[view.state]}`}>{view.reason}</div>}
      </div>
      {view.state === "error" ? (
        <div className="placeholder">Fix view.json to render this view.</div>
      ) : (
        <ViewFrame key={view.hash} view={view} params={params} dark={dark} onParams={props.onParams} onStatus={onStatus} />
      )}
      {errors.length > 0 && (
        <div className="view-errors">
          <div className="view-errors-head">
            <b>{errors.length} error{errors.length === 1 ? "" : "s"} from the view</b>
            <span className="spacer" />
            <button className="btn ghost sm" onClick={() => setErrors([])}>Clear</button>
          </div>
          {errors
            .slice()
            .reverse()
            .map((e, i) => (
              <details key={i} className="view-error">
                <summary>
                  <span className="kind">{e.phase}</span>
                  {e.file && <code>{e.file}</code>}
                  <span className="msg">{e.error}</span>
                </summary>
                {e.stack && <pre>{e.stack}</pre>}
              </details>
            ))}
        </div>
      )}
    </div>
  );
}

function ParamBar(props: { specs: ProjectParam[]; overrides: Params; onChange: (p: Params) => Promise<ParamsState> }) {
  const set = (name: string, value: unknown) => props.onChange({ [name]: value }).catch(() => {});
  return (
    <div className="param-bar">
      {props.specs.map((p) => {
        const v = p.value;
        const custom = Object.prototype.hasOwnProperty.call(props.overrides, p.name);
        return (
          <label key={p.name} className={`param ${custom ? "custom" : ""}`} title={p.description ?? ""}>
            <span className="pname">{p.name}</span>
            {p.type === "boolean" ? (
              <input type="checkbox" checked={!!v} onChange={(e) => set(p.name, e.target.checked)} />
            ) : (
              <input
                type={p.type === "date" ? "date" : p.type === "number" ? "number" : "text"}
                defaultValue={v == null ? "" : String(v)}
                key={String(v)}
                onBlur={(e) => e.target.value !== String(v ?? "") && set(p.name, p.type === "number" ? Number(e.target.value) : e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                onChange={(e) => p.type === "date" && e.target.value && set(p.name, e.target.value)}
              />
            )}
            {custom && (
              <button className="reset" onClick={() => set(p.name, null)} title={`Back to the default (${String(p.default)})`} aria-label={`Reset ${p.name} to its default`}>
                ×
              </button>
            )}
          </label>
        );
      })}
    </div>
  );
}

/** The sandboxed iframe and its bridge. Remounted when the view's files change. */
function ViewFrame(props: {
  view: ProjectView;
  params: ParamsState;
  dark: boolean;
  onParams: (p: Params) => Promise<ParamsState>;
  onStatus: (s: FrameStatus) => void;
}) {
  const ref = useRef<HTMLIFrameElement>(null);
  const bridge = useRef<ViewBridge | null>(null);
  const latest = useRef(props);
  latest.current = props;

  useEffect(() => {
    const p = latest.current;
    const b = new ViewBridge(ref.current!, p.view, p.params, p.dark, {
      setParams: (x) => latest.current.onParams(x),
      onStatus: (s) => latest.current.onStatus(s),
    });
    bridge.current = b;
    const stop = b.start();
    // Only now load the document: its bootstrap's first message must find
    // the bridge listening.
    ref.current!.srcdoc = frameDocument();
    return () => {
      stop();
      bridge.current = null;
    };
  }, []);

  useEffect(() => bridge.current?.update(props.view, props.params), [props.view, props.params]);
  useEffect(() => bridge.current?.setTheme(props.dark), [props.dark]);

  return (
    <iframe
      ref={ref}
      className="view-frame"
      title={props.view.title || props.view.name}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
    />
  );
}
