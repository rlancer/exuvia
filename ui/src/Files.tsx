import { useCallback, useEffect, useMemo, useState } from "react";
import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { api, type GraphNode, type HelperFile, type Project, type ProjectView } from "./api";
import { buildTree, countFiles, parents, projectFiles, type FileRef, type TreeDir, type TreeEntry } from "./tree";
import { HistoryBrowser } from "./History";
import { VIEW_STATE_CLASS } from "./Views";
import { language } from "./lang";
import { IconChevronRight, IconFile, IconFolder, MOD } from "./icons";

/** Unsaved edits: project-relative path → text. */
type Drafts = Record<string, string>;
// Kept outside the component, so leaving Files mode and coming back keeps
// unsaved edits, the folded folders and the open tab.
let keptDrafts: Drafts = {};
let keptCollapsed: string[] = [];
let keptTab: "code" | "history" = "code";

/**
 * Files mode: every file of the graph (nodes/, lib/) and of the views
 * (views/<name>/) as a tree, with the selected one open in an editor and its
 * saved versions a tab away. Saving goes through the same routes as the rest
 * of the workbench, so a node saved here is a node saved in the inspector.
 */
export function Files(props: {
  project: Project | null;
  dark: boolean;
  /** The open file's project-relative path. */
  file: string | null;
  onFile: (path: string | null) => void;
  onOpenNode: (name: string) => void;
  onOpenView: (name: string) => void;
}) {
  const { project, file } = props;
  const [helpers, setHelpers] = useState<HelperFile[] | null>(null);
  const [filter, setFilter] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(keptCollapsed));
  const [drafts, setDrafts] = useState<Drafts>(keptDrafts);
  useEffect(() => void (keptDrafts = drafts), [drafts]);
  useEffect(() => void (keptCollapsed = [...collapsed]), [collapsed]);

  // Unsaved edits: ask before the page goes away.
  const anyDraft = Object.keys(drafts).length > 0;
  useEffect(() => {
    if (!anyDraft) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [anyDraft]);

  // Helpers have no hash in the project: list them again on every graph event.
  useEffect(() => {
    let live = true;
    api.helpers().then(
      (r) => live && setHelpers(r.files),
      () => live && setHelpers([]),
    );
    return () => {
      live = false;
    };
  }, [project]);

  const nodes = useMemo(() => project?.nodes ?? [], [project]);
  const views = useMemo(() => project?.views ?? [], [project]);
  const refs = useMemo(() => projectFiles(nodes, views, helpers ?? []), [nodes, views, helpers]);
  const tree = useMemo(() => buildTree(refs, filter), [refs, filter]);
  const ref = refs.find((r) => r.path === file) ?? null;

  // Show the open file: unfold the folders on the way to it.
  useEffect(() => {
    if (!file) return;
    setCollapsed((c) => {
      const up = parents(file).filter((p) => c.has(p));
      if (!up.length) return c;
      const next = new Set(c);
      up.forEach((p) => next.delete(p));
      return next;
    });
  }, [file]);

  const setDraft = useCallback((path: string, text: string | null) => {
    setDrafts((d) => {
      if (text === null ? !(path in d) : d[path] === text) return d;
      const next = { ...d };
      if (text === null) delete next[path];
      else next[path] = text;
      return next;
    });
  }, []);

  const toggle = (path: string) =>
    setCollapsed((c) => {
      const next = new Set(c);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const ctx: TreeCtx = {
    open: filter.trim() ? null : collapsed,
    current: file,
    drafts,
    nodes: new Map(nodes.map((n) => [n.name, n])),
    views: new Map(views.map((v) => [v.name, v])),
    helpers: new Map((helpers ?? []).map((h) => [h.path, h])),
    toggle,
    pick: props.onFile,
  };

  return (
    <div className="files">
      <aside className="files-tree">
        <div className="files-tree-head">
          <h4>Files</h4>
          <span className="n">{refs.length}</span>
        </div>
        <input
          className="files-filter"
          type="search"
          placeholder="Filter by path…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          aria-label="Filter files by path"
        />
        <ul className="tree" role="tree" aria-label="Project files">
          {tree.map((d) => (
            <TreeItem key={d.path} entry={d} depth={0} ctx={ctx} />
          ))}
        </ul>
        {filter.trim() && !tree.length && <div className="views-empty">No file path contains “{filter.trim()}”.</div>}
      </aside>
      <section className="files-main">
        {ref ? (
          <FilePane
            key={ref.path}
            fileRef={ref}
            project={project!}
            helper={ref.kind === "lib" ? ctx.helpers.get(ref.path) : undefined}
            dark={props.dark}
            draft={drafts[ref.path]}
            onDraft={(text) => setDraft(ref.path, text)}
            onFile={props.onFile}
            onOpenNode={props.onOpenNode}
            onOpenView={props.onOpenView}
          />
        ) : (
          <div className="placeholder">
            {!project || !helpers
              ? "Loading…"
              : file
                ? `${file} is not in the project any more.`
                : "Pick a file: a node under nodes/, a helper under lib/ or one of a view's files under views/."}
          </div>
        )}
      </section>
    </div>
  );
}

interface TreeCtx {
  /** Folded folders; null while filtering, when every folder is open. */
  open: Set<string> | null;
  current: string | null;
  drafts: Drafts;
  nodes: Map<string, GraphNode>;
  views: Map<string, ProjectView>;
  helpers: Map<string, HelperFile>;
  toggle: (path: string) => void;
  pick: (path: string) => void;
}

function TreeItem({ entry, depth, ctx }: { entry: TreeEntry; depth: number; ctx: TreeCtx }) {
  const pad = { paddingLeft: 6 + depth * 14 };
  if (entry.type === "dir") return <DirItem dir={entry} depth={depth} ctx={ctx} pad={pad} />;

  const r = entry.ref;
  const node = r.kind === "node" ? ctx.nodes.get(r.node) : undefined;
  const helper = r.kind === "lib" ? ctx.helpers.get(r.path) : undefined;
  const on = ctx.current === entry.path;
  return (
    <li role="none">
      <button
        role="treeitem"
        aria-selected={on}
        className={`tree-row file ${on ? "on" : ""} ${node ? `s-${node.state}` : ""}`}
        style={pad}
        onClick={() => ctx.pick(entry.path)}
        title={node ? `${node.name} · ${node.state}${node.reason ? ` · ${node.reason}` : ""}` : entry.path}
      >
        <span className="twist" />
        {node ? <span className="dot" /> : <IconFile size={13} />}
        <span className="fname">{entry.name}</span>
        {entry.path in ctx.drafts && <span className="dirty" title="Unsaved changes">•</span>}
        {node?.parse_error && <span className="tag err" title={node.parse_error}>error</span>}
        {r.kind === "view" && r.entry && <span className="tag">entry</span>}
        {helper && <span className="tag" title={helper.used_by.length ? `Used by ${helper.used_by.join(", ")}` : "No node imports it"}>{helper.used_by.length} use{helper.used_by.length === 1 ? "" : "s"}</span>}
      </button>
    </li>
  );
}

function DirItem({ dir, depth, ctx, pad }: { dir: TreeDir; depth: number; ctx: TreeCtx; pad: { paddingLeft: number } }) {
  const expanded = !ctx.open?.has(dir.path);
  const view = dir.view ? ctx.views.get(dir.view) : undefined;
  const dirty = Object.keys(ctx.drafts).some((p) => p.startsWith(`${dir.path}/`));
  return (
    <li role="none">
      <button
        role="treeitem"
        aria-expanded={expanded}
        className={`tree-row dir ${view ? VIEW_STATE_CLASS[view.state] : ""}`}
        style={pad}
        onClick={() => ctx.open && ctx.toggle(dir.path)}
        title={view ? `${view.title || view.name} · ${view.state}${view.reason ? ` · ${view.reason}` : ""}` : `${dir.path}/`}
      >
        <span className={`twist ${expanded ? "open" : ""}`}><IconChevronRight size={10} /></span>
        {view ? <span className="dot" /> : <IconFolder size={13} />}
        <span className="fname">{dir.name}/</span>
        {!expanded && dirty && <span className="dirty" title="Unsaved changes inside">•</span>}
        <span className="count">{countFiles(dir)}</span>
      </button>
      {expanded && (
        <ul role="group">
          {dir.children.map((c) => (
            <TreeItem key={c.path} entry={c} depth={depth + 1} ctx={ctx} />
          ))}
          {!dir.children.length && (
            <li className="tree-empty" style={{ paddingLeft: 26 + depth * 14 }}>
              {dir.path === "nodes" ? "No nodes yet." : dir.path === "views" ? "No views yet." : "Empty."}
            </li>
          )}
        </ul>
      )}
    </li>
  );
}

/** Read a file's saved text. */
function load(r: FileRef): Promise<string> {
  if (r.kind === "node") return api.node(r.node).then((n) => n.source);
  if (r.kind === "lib") return api.helper(r.path).then((h) => h.source);
  return api.viewFile(r.view, r.file);
}

/** Write a file; answers a problem with the text that is still written (a helper that does not parse). */
async function store(r: FileRef, source: string): Promise<string | null> {
  if (r.kind === "node") await api.save(r.node, source);
  else if (r.kind === "view") await api.saveViewFile(r.view, r.file, source);
  else return (await api.saveHelper(r.path, source)).parse_error;
  return null;
}

/** The open file: what it is in the graph, its text in an editor and its saved versions. */
function FilePane(props: {
  fileRef: FileRef;
  project: Project;
  helper: HelperFile | undefined;
  dark: boolean;
  draft: string | undefined;
  onDraft: (text: string | null) => void;
  onFile: (path: string) => void;
  onOpenNode: (name: string) => void;
  onOpenView: (name: string) => void;
}) {
  const { fileRef: r, project, draft, onDraft } = props;
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [warn, setWarn] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTabState] = useState(keptTab);
  const setTab = (t: typeof tab) => {
    keptTab = t;
    setTabState(t);
  };

  const node = r.kind === "node" ? project.nodes.find((n) => n.name === r.node) : undefined;
  const view = r.kind === "view" ? project.views.find((v) => v.name === r.view) : undefined;
  // Read the saved text again whenever the file changes: a save here, the agent, an editor on disk.
  // A helper has no hash of its own: any graph change reads it again.
  const version = node?.code_hash ?? view?.hash ?? project;

  useEffect(() => {
    let live = true;
    load(r).then(
      (t) => live && (setSaved(t), setErr(null)),
      (e: Error) => live && setErr(e.message),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [r.path, version]);

  // An edit that now matches the saved file is no longer unsaved.
  useEffect(() => {
    if (saved !== null && draft === saved) onDraft(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saved]);

  const text = draft ?? saved ?? "";
  const dirty = draft !== undefined;

  const save = async () => {
    if (!dirty || busy) return;
    setBusy(true);
    setErr(null);
    setWarn(null);
    try {
      const source = draft;
      setWarn(await store(r, source));
      setSaved(source);
      onDraft(null);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const readBy = node
    ? [
        ...project.nodes.filter((n) => n.deps.includes(node.name)).map((n) => ({ label: n.name, path: n.path, state: `s-${n.state}` })),
        ...project.views
          .filter((v) => v.reads.includes(node.name))
          .map((v) => ({ label: `${v.name} (view)`, path: `${v.path}/${v.entry}`, state: VIEW_STATE_CLASS[v.state] })),
      ]
    : [];
  const nodePath = (name: string) => project.nodes.find((n) => n.name === name)?.path;
  const chip = (label: string, path: string | undefined, state: string, key = label) => (
    <button key={key} className={`read-chip ${state}`} disabled={!path} onClick={() => path && props.onFile(path)} title={path ? `Open ${path}` : "no such node"}>
      <span className="dot" /> {label}
    </button>
  );

  return (
    <div className="view-pane">
      <div className="view-head">
        <div className="insp-title">
          <h3 className="mono file-title">{r.path}</h3>
          {node && (
            <span className={`view-state s-${node.state}`}>
              <span className="dot" /> {node.kind} node · {node.state}
            </span>
          )}
          {r.kind === "lib" && <span className="view-state">helper</span>}
          {view && (
            <span className={`view-state ${VIEW_STATE_CLASS[view.state]}`}>
              <span className="dot" /> view {view.title || view.name} · {view.state}
            </span>
          )}
          {node && (
            <button className="btn sm" onClick={() => props.onOpenNode(node.name)} title="Select this node on the graph">
              Open on graph
            </button>
          )}
          {view && view.state !== "error" && (
            <button className="btn sm" onClick={() => props.onOpenView(view.name)} title="Render this view">
              Open view
            </button>
          )}
        </div>
        {(node?.description || view?.description) && <div className="insp-sub">{node?.description || view?.description}</div>}
        {node && (node.deps.length > 0 || readBy.length > 0) && (
          <div className="view-reads">
            {node.deps.length > 0 && <span className="lbl">reads</span>}
            {node.deps.map((d) => chip(d, nodePath(d), `s-${project.nodes.find((n) => n.name === d)?.state ?? "error"}`))}
            {readBy.length > 0 && <span className="lbl">read by</span>}
            {readBy.map((x) => chip(x.label, x.path, x.state, `by:${x.label}`))}
          </div>
        )}
        {view && view.reads.length > 0 && (
          <div className="view-reads" title="The nodes this view reads (view.json reads)">
            <span className="lbl">reads</span>
            {view.reads.map((d) => chip(d, nodePath(d), `s-${project.nodes.find((n) => n.name === d)?.state ?? "error"}`))}
          </div>
        )}
        {props.helper && (
          <div className="view-reads">
            <span className="lbl">used by</span>
            {props.helper.used_by.length ? (
              props.helper.used_by.map((d) => chip(d, nodePath(d), `s-${project.nodes.find((n) => n.name === d)?.state ?? "error"}`))
            ) : (
              <span className="hint">no node imports it</span>
            )}
          </div>
        )}
        {(node?.parse_error || (view?.parse_error && r.kind === "view" && r.file === "view.json")) && (
          <div className="err-banner"><div>{node?.parse_error ?? view?.parse_error}</div></div>
        )}
        <div className="tabs" role="tablist" aria-label={`${r.path}: its code and its history`}>
          <button role="tab" aria-selected={tab === "code"} className={`tab ${tab === "code" ? "on" : ""}`} onClick={() => setTab("code")}>
            Code{dirty && <span title="Unsaved changes"> •</span>}
          </button>
          <button role="tab" aria-selected={tab === "history"} className={`tab ${tab === "history" ? "on" : ""}`} onClick={() => setTab("history")} title="Every saved version of this file">
            History
          </button>
        </div>
      </div>
      {tab === "code" ? (
        <div
          className="code-wrap files-code"
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "s") {
              e.preventDefault();
              save();
            }
          }}
        >
          {err && <div className="err-banner"><div>{err}</div></div>}
          {warn && <div className="err-banner"><div>Saved, but {warn}</div></div>}
          {saved === null && !err ? (
            <div className="placeholder">Loading…</div>
          ) : (
            <CodeMirror
              value={text}
              onChange={(t) => onDraft(t === saved ? null : t)}
              theme={props.dark ? "dark" : "light"}
              extensions={[...language(r.path), EditorView.lineWrapping]}
              basicSetup={{ foldGutter: false, highlightActiveLine: true }}
              style={{ flex: 1, minHeight: 0, overflow: "auto" }}
              height="100%"
            />
          )}
          <div className="code-bar">
            <span className="mono">{text.split("\n").length} lines</span>
            <span className="spacer" />
            <span>{dirty ? `unsaved · ${MOD}S save` : "saved"}</span>
            {dirty && (
              <button className="btn sm ghost" onClick={() => onDraft(null)} disabled={busy} title="Throw away the unsaved edits to this file">
                Discard
              </button>
            )}
            <button className="btn sm" onClick={save} disabled={!dirty || busy}>
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      ) : (
        <div className="view-history">
          <HistoryBrowser
            filter={r.kind === "node" ? { node: r.node } : r.kind === "view" ? { view: r.view } : {}}
            tick={version}
            dark={props.dark}
            layout="side"
            path={r.path}
            restoreNote={() => (dirty ? `Your unsaved edits to ${r.path} will be replaced.` : null)}
            onRestored={() => onDraft(null)}
          />
        </div>
      )}
    </div>
  );
}
