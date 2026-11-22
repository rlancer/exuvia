import { useEffect, useMemo, useState } from "react";
import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { api, type GraphNode, type HelperFile, type Project, type ProjectView } from "./api";
import { buildTree, countFiles, parents, projectFiles, type FileRef, type TreeDir, type TreeEntry } from "./tree";
import { HistoryBrowser } from "./History";
import { VIEW_STATE_CLASS } from "./Views";
import { language } from "./lang";
import { IconChevronRight, IconClose, IconFile, IconFolder, MOD } from "./icons";
import { useAccess } from "./Access";

// Kept outside the components, so closing the panel or switching its tab
// keeps the folded folders, and closing a helper keeps its unsaved edits.
let keptCollapsed: string[] = [];
let keptFilter = "";
const helperDrafts: Record<string, string> = {};

/**
 * The Files tab of the side panel: every file of the graph (nodes/, lib/) and
 * of the views (views/<name>/) as a tree. It only finds files: picking one
 * opens it where it is edited (the node in the inspector, the view's Code tab,
 * a helper in the main area).
 */
export function FileTree(props: {
  project: Project | null;
  /** The path of the file open now, highlighted. */
  current: string | null;
  onOpen: (ref: FileRef) => void;
}) {
  const { project, current } = props;
  const [helpers, setHelpers] = useState<HelperFile[] | null>(null);
  const [filter, setFilterState] = useState(keptFilter);
  const setFilter = (f: string) => setFilterState((keptFilter = f));
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(keptCollapsed));
  useEffect(() => void (keptCollapsed = [...collapsed]), [collapsed]);

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

  // Show the open file: unfold the folders on the way to it.
  useEffect(() => {
    if (!current) return;
    setCollapsed((c) => {
      const up = parents(current).filter((p) => c.has(p));
      if (!up.length) return c;
      const next = new Set(c);
      up.forEach((p) => next.delete(p));
      return next;
    });
  }, [current]);

  const ctx: TreeCtx = {
    folded: filter.trim() ? null : collapsed,
    current,
    nodes: new Map(nodes.map((n) => [n.name, n])),
    views: new Map(views.map((v) => [v.name, v])),
    helpers: new Map((helpers ?? []).map((h) => [h.path, h])),
    toggle: (path) =>
      setCollapsed((c) => {
        const next = new Set(c);
        if (next.has(path)) next.delete(path);
        else next.add(path);
        return next;
      }),
    open: props.onOpen,
  };

  return (
    <div className="side-files">
      <input
        className="files-filter"
        type="search"
        placeholder={`Filter ${refs.length} files by path…`}
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        aria-label="Filter files by path"
      />
      <ul className="tree" role="tree" aria-label="Project files">
        {tree.map((d) => (
          <TreeItem key={d.path} entry={d} depth={0} ctx={ctx} />
        ))}
      </ul>
      {filter.trim() && !tree.length && <div className="side-empty">No file path contains “{filter.trim()}”.</div>}
      {!project && <div className="side-empty">Loading…</div>}
    </div>
  );
}

interface TreeCtx {
  /** Folded folders; null while filtering, when every folder is open. */
  folded: Set<string> | null;
  current: string | null;
  nodes: Map<string, GraphNode>;
  views: Map<string, ProjectView>;
  helpers: Map<string, HelperFile>;
  toggle: (path: string) => void;
  open: (ref: FileRef) => void;
}

function TreeItem({ entry, depth, ctx }: { entry: TreeEntry; depth: number; ctx: TreeCtx }) {
  const pad = { paddingLeft: 6 + depth * 14 };
  if (entry.type === "dir") return <DirItem dir={entry} depth={depth} ctx={ctx} pad={pad} />;

  const r = entry.ref;
  const node = r.kind === "node" ? ctx.nodes.get(r.node) : undefined;
  const helper = r.kind === "lib" ? ctx.helpers.get(r.path) : undefined;
  const on = ctx.current === entry.path;
  const where = r.kind === "node" ? "on the graph" : r.kind === "view" ? "in the view's Code tab" : "in the editor";
  return (
    <li role="none">
      <button
        role="treeitem"
        aria-selected={on}
        className={`tree-row file ${on ? "on" : ""} ${node ? `s-${node.state}` : ""}`}
        style={pad}
        onClick={() => ctx.open(r)}
        title={`${node ? `${node.name} · ${node.state}${node.reason ? ` · ${node.reason}` : ""}\n` : ""}Open ${entry.path} ${where}`}
      >
        <span className="twist" />
        {node ? <span className="dot" /> : <IconFile size={13} />}
        <span className="fname">{entry.name}</span>
        {entry.path in helperDrafts && <span className="dirty" title="Unsaved changes">•</span>}
        {node?.parse_error && <span className="tag err" title={node.parse_error}>error</span>}
        {r.kind === "view" && r.entry && <span className="tag">entry</span>}
        {helper && (
          <span className="tag" title={helper.used_by.length ? `Used by ${helper.used_by.join(", ")}` : "No node imports it"}>
            {helper.used_by.length} use{helper.used_by.length === 1 ? "" : "s"}
          </span>
        )}
      </button>
    </li>
  );
}

function DirItem({ dir, depth, ctx, pad }: { dir: TreeDir; depth: number; ctx: TreeCtx; pad: { paddingLeft: number } }) {
  const expanded = !ctx.folded?.has(dir.path);
  const view = dir.view ? ctx.views.get(dir.view) : undefined;
  return (
    <li role="none">
      <button
        role="treeitem"
        aria-expanded={expanded}
        className={`tree-row dir ${view ? VIEW_STATE_CLASS[view.state] : ""}`}
        style={pad}
        onClick={() => ctx.folded && ctx.toggle(dir.path)}
        title={view ? `${view.title || view.name} · ${view.state}${view.reason ? ` · ${view.reason}` : ""}` : `${dir.path}/`}
      >
        <span className={`twist ${expanded ? "open" : ""}`}>
          <IconChevronRight size={10} />
        </span>
        {view ? <span className="dot" /> : <IconFolder size={13} />}
        <span className="fname">{dir.name}/</span>
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

/**
 * A shared helper (lib/**.py) open in the main area: the nodes that import it,
 * its text in an editor and its saved versions. Helpers have no other home in
 * the workbench, unlike node and view files.
 */
export function HelperEditor(props: {
  path: string;
  project: Project;
  dark: boolean;
  onClose: () => void;
  onOpenNode: (name: string) => void;
}) {
  const { path, project } = props;
  const { canEdit } = useAccess();
  const [file, setFile] = useState<(HelperFile & { source: string }) | null>(null);
  const [draft, setDraftState] = useState<string | undefined>(helperDrafts[path]);
  const [err, setErr] = useState<string | null>(null);
  const [warn, setWarn] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"code" | "history">("code");
  const setDraft = (text: string | null) => {
    if (text === null) delete helperDrafts[path];
    else helperDrafts[path] = text;
    setDraftState(text ?? undefined);
  };

  // Unsaved edits: ask before the page goes away.
  useEffect(() => {
    if (draft === undefined) return;
    const stop = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", stop);
    return () => window.removeEventListener("beforeunload", stop);
  }, [draft]);

  // A helper has no hash of its own: read it again on every graph change (a save here, the agent, an editor on disk).
  useEffect(() => {
    let live = true;
    api.helper(path).then(
      (f) => live && (setFile(f), setErr(null)),
      (e: Error) => live && setErr(e.message),
    );
    return () => {
      live = false;
    };
  }, [path, project]);

  // An edit that now matches the saved file is no longer unsaved.
  useEffect(() => {
    if (file && draft === file.source) setDraft(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file]);

  const saved = file?.source ?? null;
  const text = draft ?? saved ?? "";
  const dirty = draft !== undefined;

  const save = async () => {
    if (!canEdit) return;
    if (!dirty || busy) return;
    setBusy(true);
    setErr(null);
    setWarn(null);
    try {
      const r = await api.saveHelper(path, draft);
      setWarn(r.parse_error);
      setFile((f) => (f ? { ...f, source: draft, used_by: r.used_by } : f));
      setDraft(null);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const state = (name: string) => project.nodes.find((n) => n.name === name)?.state;
  return (
    <div className="view-pane">
      <div className="view-head">
        <div className="insp-title">
          <h3 className="mono file-title">{path}</h3>
          <span className="view-state">helper</span>
          <button className="btn ghost icon-btn sm" onClick={props.onClose} title="Close and go back to the graph" aria-label="Close the helper">
            <IconClose />
          </button>
        </div>
        <div className="view-reads">
          <span className="lbl">used by</span>
          {file?.used_by.length ? (
            file.used_by.map((d) => (
              <button key={d} className={`read-chip s-${state(d) ?? "error"}`} disabled={!state(d)} onClick={() => props.onOpenNode(d)} title={`Open ${d} on the graph`}>
                <span className="dot" /> {d}
              </button>
            ))
          ) : (
            <span className="hint">{file ? "no node imports it" : "…"}</span>
          )}
        </div>
        <div className="tabs" role="tablist" aria-label={`${path}: its code and its history`}>
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
              onChange={(t) => setDraft(t === saved ? null : t)}
              editable={canEdit}
              readOnly={!canEdit}
              theme={props.dark ? "dark" : "light"}
              extensions={[...language(path), EditorView.lineWrapping]}
              basicSetup={{ foldGutter: false, highlightActiveLine: true }}
              style={{ flex: 1, minHeight: 0, overflow: "auto" }}
              height="100%"
            />
          )}
          <div className="code-bar">
            <span className="mono">{text.split("\n").length} lines</span>
            <span className="spacer" />
            <span>{!canEdit ? "read-only" : dirty ? `unsaved · ${MOD}S save` : "saved"}</span>
            {dirty && (
              <button className="btn sm ghost" onClick={() => setDraft(null)} disabled={busy} title="Throw away the unsaved edits to this file">
                Discard
              </button>
            )}
            {canEdit && (
              <button className="btn sm" onClick={save} disabled={!dirty || busy}>
                {busy ? "Saving…" : "Save"}
              </button>
            )}
          </div>
        </div>
      ) : (
        <div className="view-history">
          <HistoryBrowser filter={{}} tick={project} dark={props.dark} layout="side" path={path} />
        </div>
      )}
    </div>
  );
}
