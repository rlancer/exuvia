import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { api, type GraphNode, type Project, type ProjectParam, type ProjectView } from "./api";
import { fileOrder } from "./views/files";
import { ViewBridge, frameDocument, type FrameStatus } from "./views/host";
import type { Params, ParamsState } from "./views/protocol";
import { IconPlay, MOD } from "./icons";
import { language } from "./lang";
import { HistoryBrowser } from "./History";

/** What the main pane shows for the selected view: the rendered view, its files or their history. */
type Pane = "view" | "code" | "history";
/** Unsaved edits: view name → file path → text. */
type Drafts = Record<string, Record<string, string>>;
// Kept outside the component, so leaving Views mode for the graph and coming
// back keeps unsaved edits and the open pane.
let keptDrafts: Drafts = {};
let keptPane: Pane = "view";
let keptView: string | null = null;

/** Open Views mode on this view, rendered (from the Files mode's "Open view"). */
export function showViewNext(name: string) {
  keptView = name;
  keptPane = "view";
}

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
export const VIEW_STATE_CLASS: Record<ProjectView["state"], string> = {
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
  const [selected, setSelected] = useState<string | null>(keptView);
  const [pane, setPane] = useState<Pane>(keptPane);
  useEffect(() => void (keptView = selected), [selected]);
  const [drafts, setDrafts] = useState<Drafts>(keptDrafts);
  const [openFiles, setOpenFiles] = useState<Record<string, string>>({});
  useEffect(() => void (keptDrafts = drafts), [drafts]);
  useEffect(() => void (keptPane = pane), [pane]);
  // Unsaved edits: ask before the page goes away.
  const anyDraft = Object.keys(drafts).length > 0;
  useEffect(() => {
    if (!anyDraft) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [anyDraft]);

  /** Set (or with `null`, drop) the unsaved text of one file of one view. */
  const setDraft = useCallback((viewName: string, path: string, text: string | null) => {
    setDrafts((d) => {
      const mine = { ...d[viewName] };
      if (text === null) delete mine[path];
      else mine[path] = text;
      const next = { ...d };
      if (Object.keys(mine).length) next[viewName] = mine;
      else delete next[viewName];
      return next;
    });
  }, []);

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
              {drafts[v.name] && <span className="dirty" title="Unsaved changes">•</span>}
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
            pane={pane}
            onPane={setPane}
            drafts={drafts[view.name] ?? {}}
            onDraft={(path, text) => setDraft(view.name, path, text)}
            file={openFiles[view.name]}
            onFile={(path) => setOpenFiles((f) => ({ ...f, [view.name]: path }))}
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
  pane: Pane;
  onPane: (p: Pane) => void;
  drafts: Record<string, string>;
  onDraft: (path: string, text: string | null) => void;
  file: string | undefined;
  onFile: (path: string) => void;
}) {
  const { view, params, dark, pane } = props;
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

  /** Show one of the view's files in the Code pane. */
  const openFile = (path: string) => {
    props.onFile(path);
    props.onPane("code");
  };

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
        <div className="tabs" role="tablist" aria-label={`${view.name}: the rendered view, its code and its history`}>
          <button role="tab" aria-selected={pane === "view"} className={`tab ${pane === "view" ? "on" : ""}`} onClick={() => props.onPane("view")}>
            View
          </button>
          <button role="tab" aria-selected={pane === "code"} className={`tab ${pane === "code" ? "on" : ""}`} onClick={() => props.onPane("code")}>
            Code{Object.keys(props.drafts).length > 0 && <span title="Unsaved changes"> •</span>}
            <span className="n">{view.files.length}</span>
          </button>
          <button role="tab" aria-selected={pane === "history"} className={`tab ${pane === "history" ? "on" : ""}`} onClick={() => props.onPane("history")} title="Every saved version of this view's files, deleted ones too">
            History
          </button>
        </div>
      </div>
      {pane === "code" && (
        <ViewCode
          key={view.name}
          view={view}
          dark={dark}
          drafts={props.drafts}
          onDraft={props.onDraft}
          file={props.file}
          onFile={props.onFile}
          onHistory={() => props.onPane("history")}
        />
      )}
      {pane === "history" && (
        <div className="view-history">
          <HistoryBrowser
            filter={{ view: view.name }}
            tick={view.hash}
            dark={dark}
            layout="side"
            path={props.file ? `${view.path}/${props.file}` : undefined}
            restoreNote={(rev) => {
              const rel = rev.path.slice(view.path.length + 1);
              return rel in props.drafts ? `Your unsaved edits to ${rel} will be replaced.` : null;
            }}
            onRestored={(rev) => props.onDraft(rev.path.slice(view.path.length + 1), null)}
          />
        </div>
      )}
      {view.state === "error" ? (
        pane === "view" && (
          <div className="placeholder">
            Fix view.json to render this view.{" "}
            <button className="btn sm" onClick={() => openFile("view.json")}>Open view.json</button>
          </div>
        )
      ) : (
        // Stays mounted while the code is open, so saving a file rebuilds it and
        // its compile and runtime errors show up under the editor.
        <ViewFrame key={view.hash} view={view} params={params} dark={dark} hidden={pane !== "view"} onParams={props.onParams} onStatus={onStatus} />
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
                  {e.file && (
                    <button
                      className="file-link"
                      title={`Open ${e.file}`}
                      onClick={(ev) => {
                        ev.preventDefault(); // don't also open the stack
                        openFile(e.file!);
                      }}
                    >
                      <code>{e.file}</code>
                    </button>
                  )}
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

/**
 * The Code pane: the view's files, one open in an editor. Saving writes the
 * file (PUT /api/views/{name}/files/{path}); the new hash rebuilds the frame.
 */
function ViewCode(props: {
  view: ProjectView;
  dark: boolean;
  drafts: Record<string, string>;
  onDraft: (path: string, text: string | null) => void;
  file: string | undefined;
  onFile: (path: string) => void;
  /** Open the History tab (on the file shown here). */
  onHistory: () => void;
}) {
  const { view, drafts, onDraft } = props;
  const [saved, setSaved] = useState<Record<string, string> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // The files as saved, read again whenever they change: a save here, the
  // agent, or an editor on disk. Unsaved edits are kept.
  useEffect(() => {
    let live = true;
    api.view(view.name).then(
      (r) => live && setSaved(Object.fromEntries(r.sources.map((f) => [f.path, f.source]))),
      (e: Error) => live && setErr(e.message),
    );
    return () => {
      live = false;
    };
  }, [view.name, view.hash]);

  // An edit that now matches the saved file is no longer unsaved.
  useEffect(() => {
    if (!saved) return;
    for (const [path, text] of Object.entries(drafts)) if (saved[path] === text) onDraft(path, null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saved]);

  // A file deleted elsewhere while it has unsaved edits stays listed: saving brings it back.
  const files = fileOrder([...Object.keys(saved ?? {}), ...Object.keys(drafts)], view.entry);
  const current = props.file && files.includes(props.file) ? props.file : files.includes(view.entry) ? view.entry : files[0];
  const text = current ? (drafts[current] ?? saved?.[current] ?? "") : "";
  const dirty = !!current && current in drafts;
  const unsaved = files.filter((f) => f in drafts);

  const save = async (paths: string[]) => {
    if (!paths.length || busy) return;
    setBusy(true);
    setErr(null);
    try {
      for (const path of paths) {
        const source = drafts[path];
        await api.saveViewFile(view.name, path, source);
        setSaved((s) => ({ ...s, [path]: source }));
        onDraft(path, null);
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="view-code">
      <nav className="view-files" aria-label={`Files of ${view.name}`}>
        {files.map((f) => (
          <button key={f} className={`view-file ${f === current ? "on" : ""}`} aria-current={f === current} onClick={() => props.onFile(f)} title={f}>
            <span className="fname">{f}</span>
            {f in drafts && <span className="dirty" title="Unsaved changes">•</span>}
          </button>
        ))}
      </nav>
      <div
        className="code-wrap"
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === "s") {
            e.preventDefault();
            if (dirty) save([current!]);
          }
        }}
      >
        {err && <div className="err-banner"><div>{err}</div></div>}
        {current ? (
          <CodeMirror
            key={current}
            value={text}
            onChange={(t) => onDraft(current, t === saved?.[current] ? null : t)}
            theme={props.dark ? "dark" : "light"}
            extensions={[...language(current), EditorView.lineWrapping]}
            basicSetup={{ foldGutter: false, highlightActiveLine: true }}
            style={{ flex: 1, minHeight: 0, overflow: "auto" }}
            height="100%"
          />
        ) : (
          <div className="placeholder">{saved ? "This view has no files." : "Loading…"}</div>
        )}
        <div className="code-bar">
          <span className="mono">{current ? `${view.path}/${current}` : view.path}</span>
          <span className="spacer" />
          <span>{dirty ? `unsaved · ${MOD}S save` : "saved"}</span>
          <button
            className="btn sm ghost"
            onClick={() => {
              if (current) props.onFile(current);
              props.onHistory();
            }}
            title={current ? `Every saved version of ${current}` : "Every saved version of this view's files"}
          >
            History
          </button>
          {dirty && (
            <button className="btn sm ghost" onClick={() => onDraft(current!, null)} disabled={busy} title="Throw away the unsaved edits to this file">
              Discard
            </button>
          )}
          {unsaved.length > 1 && (
            <button className="btn sm" onClick={() => save(unsaved)} disabled={busy}>
              Save all ({unsaved.length})
            </button>
          )}
          <button className="btn sm" onClick={() => save([current!])} disabled={!dirty || busy}>
            Save
          </button>
        </div>
      </div>
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
  /** Kept running but not shown (while the Code pane is open). */
  hidden: boolean;
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
      hidden={props.hidden}
    />
  );
}
