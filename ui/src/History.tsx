import { useEffect, useMemo, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { EditorState } from "@uiw/react-codemirror";
import { unifiedMergeView } from "@codemirror/merge";
import { colorVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { List, ListItem } from "@astryxdesign/core/List";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { api, type CodeRevision } from "./api";
import { currentVersion, deletedFiles, isDeleted, ORIGIN_LABEL, paths, previousVersion, restoreTarget } from "./revisions";
import { language } from "./lang";
import { ago } from "./util";
import { useAccess } from "./Access";
import { CodeEditor } from "./ui/CodeEditor";

/** What the selected version is compared with. */
type Against = "current" | "previous";

const when = (ts: number) => new Date(ts * 1000).toLocaleString();

/** The file filter's "every file" choice: no path is spelled like it. */
const ALL_FILES = "*";

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
  const { canEdit } = useAccess();
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
      setConfirm(null);
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!revs)
    return (
      <div {...stylex.props(styles.frame, styles.alone)}>
        {err ? <Banner status="error" container="section" title={err} /> : <Note>Loading history…</Note>}
      </div>
    );
  if (!all.length)
    return (
      <div {...stylex.props(styles.frame, styles.alone)}>
        <div {...stylex.props(styles.note)}>
          <EmptyState isCompact title="No saved versions yet" description="Every save of the code is kept here." />
        </div>
      </div>
    );

  const multi = files.length > 1;
  const oneFile = !!path && files.includes(path) && !deletedOnly;
  const side = props.layout === "side";
  return (
    <div {...stylex.props(styles.frame, styles.history, side ? styles.side : styles.stack)}>
      <div {...stylex.props(styles.list)}>
        {(multi || props.deletedFilter) && (
          <div {...stylex.props(styles.bar)}>
            {props.deletedFilter && (
              <SegmentedControl size="sm" label="Which revisions" value={deletedOnly ? "deleted" : "all"} onChange={(v) => setDeletedOnly(v === "deleted")} xstyle={styles.fixed}>
                <SegmentedControlItem value="all" label="All" />
                <SegmentedControlItem value="deleted" label={deleted.length ? `Deleted (${deleted.length})` : "Deleted"} />
              </SegmentedControl>
            )}
            {multi && !deletedOnly && (
              <Selector
                size="sm"
                label="Show the history of one file"
                isLabelHidden
                hasSearch={files.length > 8}
                searchPlaceholder="Find a file…"
                value={files.includes(path) ? path : ALL_FILES}
                onChange={(v) => setPath(!v || v === ALL_FILES ? "" : v)}
                options={[
                  { value: ALL_FILES, label: `All files (${files.length})` },
                  ...files.map((f) => ({ value: f, label: `${f}${isDeleted(all, f) ? " (deleted)" : ""}` })),
                ]}
                containerXstyle={styles.grow}
              />
            )}
          </div>
        )}
        <div {...stylex.props(styles.revs)}>
          <List density="compact" hasDividers aria-label="Saved versions">
            {shown.map((r) => (
              <ListItem
                key={r.id}
                isSelected={r.id === sel?.id}
                onClick={() => setSelId(r.id)}
                label={<Saved rev={r} />}
                description={
                  (multi || deletedOnly) && !oneFile ? (
                    <Text type="code" size="sm" color="secondary" maxLines={1} hasTruncateTooltip={false}>
                      {r.path}
                    </Text>
                  ) : undefined
                }
                endContent={
                  r.hash === null || (deletedOnly && isDeleted(all, r.path)) ? (
                    <Badge variant="error" label="deleted" />
                  ) : r.current ? (
                    <Badge label="current" />
                  ) : undefined
                }
              />
            ))}
          </List>
          {!shown.length && <Note>{deletedOnly ? "No deleted nodes, views or files." : "No saved versions."}</Note>}
        </div>
      </div>
      <div {...stylex.props(styles.diff)}>
        {err && <Banner status="error" container="section" title={err} />}
        {sel && (
          // What the version is compared with, and the one thing done to it.
          <div {...stylex.props(styles.bar)}>
            <Selector
              size="sm"
              label="Compare with"
              isLabelHidden
              width={132}
              value={against}
              onChange={(v) => setAgainst(v === "current" ? "current" : "previous")}
              options={[
                { value: "previous", label: "vs previous" },
                { value: "current", label: "vs current" },
              ]}
            />
            <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.grow}>
              {sel.hash === null ? "this revision deleted the file" : baseLabel}
            </Text>
            {target && canEdit ? (
              <Button
                size="sm"
                variant={canRestore ? "primary" : "secondary"}
                label={fileGone && sel.hash ? (target.kind === "node" ? "Restore node" : "Restore file") : "Restore"}
                tooltip={sel.current ? "This is the current version" : sel.hash ? "Make this version current again" : "Pick a saved version to restore"}
                isDisabled={!canRestore || busy}
                onClick={() => setConfirm(sel)}
              />
            ) : (
              <Tooltip content={canEdit ? "Only node and view files can be restored from the workbench" : "You can view this project but not change it"}>
                <Text type="supporting" color="secondary">
                  read-only
                </Text>
              </Tooltip>
            )}
          </div>
        )}
        {sel && ready ? (
          <CodeEditor
            // The merge view reads `original` once: a new pair is a new editor.
            key={`${sel.id}:${base?.id ?? "none"}`}
            value={doc}
            editable={false}
            activeLine={false}
            dark={dark}
            extensions={[
              ...language(sel.path),
              EditorState.readOnly.of(true),
              ...(same || !base ? [] : [unifiedMergeView({ original, mergeControls: false, collapseUnchanged: { margin: 3, minSize: 6 } })]),
            ]}
          />
        ) : (
          <Note>{sel ? "Loading…" : "Pick a version."}</Note>
        )}
        {sel && (
          <div {...stylex.props(styles.status)}>
            <span title={sel.hash ?? undefined} {...stylex.props(styles.grow)}>
              <Text type="code" size="sm" color="secondary" maxLines={1} hasTruncateTooltip={false}>
                {sel.path}
              </Text>
            </span>
            <span title={when(sel.saved_at)}>
              <Text type="supporting" color="secondary" hasTabularNumbers>
                {sel.hash ? `saved ${ago(sel.saved_at)}` : `deleted ${ago(sel.saved_at)}`}
              </Text>
            </span>
          </div>
        )}
      </div>
      {confirm && (
        <AlertDialog
          isOpen
          onOpenChange={(open) => {
            if (!open && !busy) setConfirm(null);
          }}
          title={`Restore ${confirm.path}?`}
          description={
            `This makes the version saved ${ago(confirm.saved_at)}${confirm.author ? ` by ${confirm.author}` : ""} (${when(confirm.saved_at)}) the current code` +
            `${isDeleted(all, confirm.path) ? ", bringing the deleted file back" : ""}. The code there now stays in the history, so this can be undone.` +
            `${props.restoreNote?.(confirm) ? ` ${props.restoreNote(confirm)}` : ""}`
          }
          actionLabel="Restore"
          actionVariant="primary"
          isActionLoading={busy}
          onAction={restore}
        />
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
    <div {...stylex.props(styles.rail)}>
      <div {...stylex.props(styles.bar)}>
        <SegmentedControl size="sm" layout="fill" label="Which revisions" value={deletedOnly ? "deleted" : "latest"} onChange={(v) => setDeletedOnly(v === "deleted")} xstyle={styles.grow}>
          <SegmentedControlItem value="latest" label="Latest saves" />
          <SegmentedControlItem value="deleted" label={deleted.length ? `Deleted (${deleted.length})` : "Deleted"} />
        </SegmentedControl>
      </div>
      {err && <Banner status="error" container="section" title={err} />}
      <div {...stylex.props(styles.revs)}>
        <List density="compact" hasDividers aria-label={deletedOnly ? "Deleted files" : "Latest saves"}>
          {shown.map(({ rev: r, gone }) => (
            <ListItem
              key={r.id}
              isSelected={r.id === props.current}
              onClick={() => props.onOpen(r)}
              label={
                // The file is what a save is told by here; when and by whom comes second.
                <span title={gone ? `Deleted ${when(gone.saved_at)}: open its last version to restore it` : `${r.path}, saved ${when(r.saved_at)}`}>
                  <Text type="code" maxLines={1} hasTruncateTooltip={false}>
                    {r.path}
                  </Text>
                </span>
              }
              description={<Saved rev={gone ?? r} quiet />}
              endContent={
                gone || r.hash === null ? <Badge variant="error" label="deleted" /> : r.current ? <Badge label="current" /> : undefined
              }
            />
          ))}
        </List>
        {revs && !shown.length && <Note>{deletedOnly ? "No deleted nodes, views or files." : "No saved versions yet. Every save of the code is kept here."}</Note>}
        {!revs && !err && <Note>Loading…</Note>}
      </div>
    </div>
  );
}

/**
 * When a revision was saved, by whom and through what, on one line. `quiet`
 * sets all of it in the secondary colour, for under a row's own label.
 */
function Saved({ rev, quiet = false }: { rev: CodeRevision; quiet?: boolean }) {
  return (
    <span {...stylex.props(styles.saved)}>
      <span title={when(rev.saved_at)} {...stylex.props(styles.when)}>
        <Text type={quiet ? "supporting" : "body"} color={quiet ? "secondary" : "primary"} hasTabularNumbers>
          {ago(rev.saved_at)}
        </Text>
      </span>
      {rev.author && (
        <Text type={quiet ? "supporting" : "body"} color={quiet ? "secondary" : "primary"} maxLines={1}>
          {rev.author}
        </Text>
      )}
      <span title={`saved through ${rev.origin}`} {...stylex.props(styles.origin)}>
        <Text type="supporting" color="secondary" maxLines={1} hasTruncateTooltip={false}>
          {ORIGIN_LABEL[rev.origin] ?? rev.origin}
        </Text>
      </span>
    </span>
  );
}

/** A line in place of a list or a diff: loading, or nothing to show. */
function Note({ children }: { children: ReactNode }) {
  return (
    <div {...stylex.props(styles.note)}>
      <Text color="secondary" justify="center">
        {children}
      </Text>
    </div>
  );
}

/**
 * The history's frame: the list of versions and the diff of the picked one.
 *
 *   side    list 300 | diff     (the main area)
 *   stack   list over diff      (a narrow pane: a node's or a view's History tab)
 *   <= 900px `side` stacks too
 *
 * The bar over the list and the bar over the diff are as tall as each other,
 * so side by side their rules meet in one line.
 */
const COMPACT = "@media (max-width: 900px)";

const styles = stylex.create({
  // It fills what it is in, a column that grows or a box with a height.
  frame: { flexGrow: 1, minWidth: 0, minHeight: 0, height: "100%" },
  alone: { display: "flex", flexDirection: "column" },
  // The rule between the list and the diff is the gap between them, in the
  // rule's colour: it is there whichever way the two are laid out.
  history: { display: "grid", gap: "1px", backgroundColor: colorVars["--color-border"] },
  stack: { gridTemplateColumns: "minmax(0, 1fr)", gridTemplateRows: "fit-content(34%) minmax(0, 1fr)" },
  side: {
    gridTemplateColumns: { default: "300px minmax(0, 1fr)", [COMPACT]: "minmax(0, 1fr)" },
    gridTemplateRows: { default: "minmax(0, 1fr)", [COMPACT]: "fit-content(36%) minmax(0, 1fr)" },
  },
  list: { display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, backgroundColor: colorVars["--color-background-surface"] },
  bar: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    flexShrink: 0,
    boxSizing: "border-box",
    minHeight: "40px",
    paddingBlock: spacingVars["--spacing-1-5"],
    paddingInline: spacingVars["--spacing-2"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  grow: { flexGrow: 1, minWidth: 0 },
  // The switch keeps its words; the file's name beside it is what gets cut.
  fixed: { flexShrink: 0 },
  revs: { flexGrow: 1, minHeight: 0, overflowY: "auto" },
  saved: { display: "flex", alignItems: "baseline", gap: spacingVars["--spacing-2"], minWidth: 0 },
  when: { flexShrink: 0 },
  origin: { minWidth: 0 },
  diff: { display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, backgroundColor: colorVars["--color-background-surface"] },
  note: { display: "grid", placeItems: "center", flexGrow: 1, minHeight: 0, padding: spacingVars["--spacing-6"] },
  status: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-3"],
    flexShrink: 0,
    paddingBlock: spacingVars["--spacing-1"],
    paddingInline: spacingVars["--spacing-2"],
    borderTopWidth: "1px",
    borderTopStyle: "solid",
    borderTopColor: colorVars["--color-border"],
  },
  // The History tab: the switch stays put, the saves under it scroll.
  rail: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: 0, minHeight: 0 },
});
