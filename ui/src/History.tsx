import { useEffect, useMemo, useState } from "react";
import CodeMirror, { EditorState, EditorView } from "@uiw/react-codemirror";
import { unifiedMergeView } from "@codemirror/merge";
import { api, type CodeRevision } from "./api";
import { Dialog } from "./Dialog";
import { currentVersion, deletedFiles, isDeleted, ORIGIN_LABEL, paths, previousVersion, restoreTarget } from "./revisions";
import { language } from "./lang";
import { ago, pressable } from "./util";

/** What the selected version is compared with. */
type Against = "current" | "previous";

const when = (ts: number) => new Date(ts * 1000).toLocaleString();

/**
 * Saved versions of some code files (a node's, a view's, or the whole
 * project's), newest first: pick one to see it as a diff against the file now
 * or against the version before it, and restore it. A restore is itself a new
 * revision, so history only grows.
 */
export function HistoryBrowser(props: {
  /** `node` or `view` narrows the list; neither lists every file of the project. */
  filter: { node?: string; view?: string };
  /** Changes when the files change (a code or view hash): the list is read again. */
  tick: unknown;
  dark: boolean;
  /** `stack`: the list above the diff (a narrow pane); `side`: next to it. */
  layout: "stack" | "side";
  /** Open with the list narrowed to this file. */
  path?: string;
  /** Open with this revision selected. */
  rev?: number;
  /** Offer a "Deleted" filter: files whose newest revision deleted them. */
  deletedFilter?: boolean;
  /** A note for the restore confirmation, e.g. that unsaved edits will be replaced. */
  restoreNote?: (rev: CodeRevision) => string | null;
  onRestored?: (rev: CodeRevision) => void;
}) {
  const { filter, dark } = props;
  const [revs, setRevs] = useState<CodeRevision[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [selId, setSelId] = useState<number | null>(props.rev ?? null);
  const [against, setAgainst] = useState<Against>("previous");
  const [path, setPath] = useState<string>(props.path ?? "");
  const [deletedOnly, setDeletedOnly] = useState(false);
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState<CodeRevision | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    api.codeRevisions({ ...filter, limit: 1000 }).then(
      (r) => live && (setRevs(r), setErr(null)),
      (e: Error) => live && setErr(e.message),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter.node, filter.view, props.tick, reload]);

  // Follow the file and the version the caller has open.
  useEffect(() => setPath(props.path ?? ""), [props.path]);
  useEffect(() => {
    if (props.rev != null) {
      setSelId(props.rev);
      setDeletedOnly(false);
    }
  }, [props.rev]);

  const all = revs ?? [];
  const files = useMemo(() => paths(all), [all]);
  const deleted = useMemo(() => deletedFiles(all), [all]);
  const shown = useMemo(() => {
    if (deletedOnly) return deleted.map((d) => d.last ?? d.deleted);
    return path && files.includes(path) ? all.filter((r) => r.path === path) : all;
  }, [all, deletedOnly, deleted, path, files]);
  const sel = shown.find((r) => r.id === selId) ?? shown[0] ?? null;

  // What the selected version is compared with.
  const cur = sel ? currentVersion(all, sel.path) : null;
  const prev = sel ? previousVersion(all, sel) : null;
  const base: CodeRevision | null = against === "current" ? cur : prev;
  const fileGone = sel ? isDeleted(all, sel.path) : false;

  // Fetch the texts the diff needs (each version once).
  useEffect(() => {
    for (const h of [sel?.hash, base?.hash]) {
      if (!h || h in texts) continue;
      api.codeBlob(h).then(
        (t) => setTexts((x) => ({ ...x, [h]: t })),
        (e: Error) => setErr(e.message),
      );
    }
  }, [sel?.hash, base?.hash, texts]);

  const doc = sel?.hash ? texts[sel.hash] : "";
  const original = base?.hash ? texts[base.hash] : "";
  const ready = sel != null && doc !== undefined && original !== undefined;
  const same = !!sel && !!base && sel.hash === base.hash;

  const baseLabel =
    against === "current"
      ? cur
        ? same
          ? "this is the current version"
          : `against the current version (${ago(cur.saved_at)})`
        : fileGone
          ? "against nothing: the file is deleted now"
          : "against nothing: the file now is not in this history"
      : prev
        ? `against the version before (${ago(prev.saved_at)})`
        : "the first version in this history";

  const target = sel ? restoreTarget(sel) : null;
  const canRestore = !!sel && !!sel.hash && !!target && !sel.current;

  const restore = async () => {
    const rev = confirm;
    if (!rev?.hash) return;
    const t = restoreTarget(rev);
    if (!t) return;
    setBusy(true);
    setErr(null);
    try {
      if (t.kind === "node") await api.restoreNode(t.name, rev.hash);
      else await api.restoreViewFile(t.name, t.path, rev.hash);
      setConfirm(null);
      setDeletedOnly(false);
      setReload((n) => n + 1);
      props.onRestored?.(rev);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!revs) return err ? <div className="err-banner"><div>{err}</div></div> : <div className="placeholder">Loading history…</div>;
  if (!all.length) return <div className="placeholder">No saved versions yet. Every save of the code is kept here.</div>;

  const multi = files.length > 1;
  return (
    <div className={`history ${props.layout}`}>
      <div className="history-list">
        {(multi || props.deletedFilter) && (
          <div className="history-filter">
            {props.deletedFilter && (
              <div className="seg" role="tablist" aria-label="Which revisions">
                <button role="tab" aria-selected={!deletedOnly} className={!deletedOnly ? "on" : ""} onClick={() => setDeletedOnly(false)}>
                  All
                </button>
                <button role="tab" aria-selected={deletedOnly} className={deletedOnly ? "on" : ""} onClick={() => setDeletedOnly(true)}>
                  Deleted{deleted.length ? <span className="n">{deleted.length}</span> : null}
                </button>
              </div>
            )}
            {multi && !deletedOnly && (
              <select value={files.includes(path) ? path : ""} onChange={(e) => setPath(e.target.value)} aria-label="Show the history of one file">
                <option value="">All files ({files.length})</option>
                {files.map((f) => (
                  <option key={f} value={f}>
                    {f}
                    {isDeleted(all, f) ? " (deleted)" : ""}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}
        <ul className="runs history-revs" aria-label="Saved versions">
          {shown.map((r) => (
            <li key={r.id} className={r.id === sel?.id ? "on" : ""} {...pressable(() => setSelId(r.id))} aria-pressed={r.id === sel?.id}>
              <span className="rev-line">
                <span className="rev-when" title={when(r.saved_at)}>{ago(r.saved_at)}</span>
                <span className="rev-who">{r.author ?? "—"}</span>
                <span className="rev-origin" title={`saved through ${r.origin}`}>{ORIGIN_LABEL[r.origin] ?? r.origin}</span>
                {r.current && <span className="rev-tag cur">current</span>}
                {(r.hash === null || (deletedOnly && isDeleted(all, r.path))) && <span className="rev-tag del">deleted</span>}
              </span>
              {(multi || deletedOnly) && !(path && files.includes(path) && !deletedOnly) && <span className="rev-path mono">{r.path}</span>}
            </li>
          ))}
          {!shown.length && <li className="none">{deletedOnly ? "No deleted nodes, views or files." : "No saved versions."}</li>}
        </ul>
      </div>
      <div className="history-diff code-wrap">
        {err && <div className="err-banner"><div>{err}</div></div>}
        {sel && (
          <div className="history-head">
            <div className="seg" role="tablist" aria-label="Compare with">
              <button role="tab" aria-selected={against === "previous"} className={against === "previous" ? "on" : ""} onClick={() => setAgainst("previous")}>
                vs previous
              </button>
              <button role="tab" aria-selected={against === "current"} className={against === "current" ? "on" : ""} onClick={() => setAgainst("current")}>
                vs current
              </button>
            </div>
            <span className="hint">{sel.hash === null ? "this revision deleted the file" : baseLabel}</span>
          </div>
        )}
        {sel && ready ? (
          <CodeMirror
            // The merge view reads `original` once: a new pair is a new editor.
            key={`${sel.id}:${base?.id ?? "none"}`}
            value={doc}
            editable={false}
            theme={dark ? "dark" : "light"}
            extensions={[
              ...language(sel.path),
              EditorView.lineWrapping,
              EditorState.readOnly.of(true),
              ...(same || !base ? [] : [unifiedMergeView({ original, mergeControls: false, collapseUnchanged: { margin: 3, minSize: 6 } })]),
            ]}
            basicSetup={{ foldGutter: false, highlightActiveLine: false }}
            style={{ flex: 1, minHeight: 0, overflow: "auto" }}
            height="100%"
          />
        ) : (
          <div className="placeholder">{sel ? "Loading…" : "Pick a version."}</div>
        )}
        {sel && (
          <div className="code-bar">
            <span className="mono" title={sel.hash ?? undefined}>{sel.path}</span>
            <span className="spacer" />
            <span title={when(sel.saved_at)}>{sel.hash ? `saved ${ago(sel.saved_at)}` : `deleted ${ago(sel.saved_at)}`}</span>
            {target ? (
              <button
                className="btn sm"
                disabled={!canRestore || busy}
                onClick={() => setConfirm(sel)}
                title={sel.current ? "This is the current version" : sel.hash ? "Make this version current again" : "Pick a saved version to restore"}
              >
                {fileGone && sel.hash ? (target.kind === "node" ? "Restore node" : "Restore file") : "Restore"}
              </button>
            ) : (
              <span className="hint" title="Only node and view files can be restored from the workbench">read-only</span>
            )}
          </div>
        )}
      </div>
      {confirm && (
        <Dialog title={`Restore ${confirm.path}?`} onClose={() => !busy && setConfirm(null)}>
          <p className="hub-muted">
            This makes the version saved {ago(confirm.saved_at)}
            {confirm.author ? ` by ${confirm.author}` : ""} ({when(confirm.saved_at)}) the current code
            {isDeleted(all, confirm.path) ? ", bringing the deleted file back" : ""}. The code there now stays in the history, so
            this can be undone.
          </p>
          {props.restoreNote?.(confirm) && <p className="hub-muted">{props.restoreNote(confirm)}</p>}
          <div className="actions">
            <button type="button" className="btn ghost" onClick={() => setConfirm(null)} disabled={busy}>Cancel</button>
            <button type="button" className="btn primary" onClick={restore} disabled={busy}>
              {busy ? "Restoring…" : "Restore"}
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

/**
 * The History tab of the side panel: the project's latest saves, newest first,
 * or the files whose newest revision deleted them. Picking one opens that
 * file's versions next to the graph, where it can be compared and restored.
 */
export function HistoryRail(props: {
  /** Changes when the files change (a code or view hash): the list is read again. */
  tick: unknown;
  /** The revision open in the main area, highlighted. */
  current: number | null;
  onOpen: (rev: CodeRevision) => void;
}) {
  const [revs, setRevs] = useState<CodeRevision[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [deletedOnly, setDeletedOnly] = useState(false);

  useEffect(() => {
    let live = true;
    api.codeRevisions({ limit: 1000 }).then(
      (r) => live && (setRevs(r), setErr(null)),
      (e: Error) => live && setErr(e.message),
    );
    return () => {
      live = false;
    };
  }, [props.tick]);

  const all = revs ?? [];
  const deleted = useMemo(() => deletedFiles(all), [all]);
  // A deleted file opens on its last saved version: the one to restore.
  const shown = deletedOnly ? deleted.map((d) => ({ rev: d.last ?? d.deleted, gone: d.deleted })) : all.slice(0, 200).map((r) => ({ rev: r, gone: null }));

  return (
    <div className="side-history">
      <div className="history-filter">
        <div className="seg" role="tablist" aria-label="Which revisions">
          <button role="tab" aria-selected={!deletedOnly} className={!deletedOnly ? "on" : ""} onClick={() => setDeletedOnly(false)}>
            Latest saves
          </button>
          <button role="tab" aria-selected={deletedOnly} className={deletedOnly ? "on" : ""} onClick={() => setDeletedOnly(true)}>
            Deleted{deleted.length ? <span className="n">{deleted.length}</span> : null}
          </button>
        </div>
      </div>
      {err && <div className="err-banner"><div>{err}</div></div>}
      <ul className="runs history-revs" aria-label={deletedOnly ? "Deleted files" : "Latest saves"}>
        {shown.map(({ rev: r, gone }) => (
          <li
            key={r.id}
            className={r.id === props.current ? "on" : ""}
            {...pressable(() => props.onOpen(r))}
            aria-pressed={r.id === props.current}
            title={gone ? `Deleted ${when(gone.saved_at)}: open its last version to restore it` : `${r.path}, saved ${when(r.saved_at)}`}
          >
            <span className="rev-path mono">{r.path}</span>
            <span className="rev-line">
              <span className="rev-when">{ago((gone ?? r).saved_at)}</span>
              <span className="rev-who">{(gone ?? r).author ?? "—"}</span>
              <span className="rev-origin">{ORIGIN_LABEL[(gone ?? r).origin] ?? (gone ?? r).origin}</span>
              {gone || r.hash === null ? <span className="rev-tag del">deleted</span> : r.current ? <span className="rev-tag cur">current</span> : null}
            </span>
          </li>
        ))}
        {revs && !shown.length && <li className="none">{deletedOnly ? "No deleted nodes, views or files." : "No saved versions yet. Every save of the code is kept here."}</li>}
        {!revs && !err && <li className="none">Loading…</li>}
      </ul>
    </div>
  );
}
