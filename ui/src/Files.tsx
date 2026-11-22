import { useEffect, useMemo, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, durationVars, focusVars, radiusVars, sizeVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import { IconButton } from "@astryxdesign/core/IconButton";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Token } from "@astryxdesign/core/Token";
import { useTreeFocus } from "@astryxdesign/core/hooks";
import { api, type GraphNode, type HelperFile, type Project, type ProjectView } from "./api";
import { buildTree, countFiles, parents, projectFiles, type FileRef, type TreeDir, type TreeEntry, type TreeFile } from "./tree";
import { HistoryBrowser } from "./History";
import { language } from "./lang";
import { IconChevronRight, IconClose, IconDownload, IconFile, IconFolder, MOD } from "./icons";
import { codeArchiveUrl, saveUrl } from "./download";
import { useAccess } from "./Access";
import { CodeEditor } from "./ui/CodeEditor";
import { StateDot } from "./ui/state";

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
 *
 * The tree is the app's own rows on Astryx's tree keyboard model
 * (`useTreeFocus`): the arrows walk the rows and fold the folders, Enter opens.
 * Astryx's TreeList keeps what is folded to itself, and this tree has to
 * unfold the way to the open file, open every folder while filtering and keep
 * the folded folders when the panel closes.
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

  // The rows on screen, top to bottom: a folded folder's files are not among them.
  const rows = visibleRows(tree, ctx.folded);
  const entries = new Map(rows.flatMap((r) => (r.kind === "entry" ? [[r.entry.path, r.entry] as const] : [])));
  const { treeRef, handleKeyDown, handleFocus } = useTreeFocus<HTMLDivElement>({
    hasRovingTabIndex: true,
    onToggleExpand: (path) => ctx.folded && ctx.toggle(path),
    // Enter or Space on a file opens it; on a folder it folds or unfolds it.
    onActivate: (_row, path) => {
      const entry = path ? entries.get(path) : undefined;
      if (entry?.type !== "file") return false;
      ctx.open(entry.ref);
      return true;
    },
  });

  return (
    <div {...stylex.props(styles.files)}>
      <div {...stylex.props(styles.filesBar)}>
        <TextInput
          size="sm"
          label="Filter files by path"
          isLabelHidden
          startIcon="search"
          hasClear
          placeholder={`Filter ${refs.length} files by path…`}
          value={filter}
          onChange={setFilter}
          containerXstyle={styles.grow}
        />
        <IconButton
          size="sm"
          variant="ghost"
          label="Download every file as a .zip"
          tooltip="Download every file as a .zip"
          icon={<IconDownload />}
          onClick={() => saveUrl(codeArchiveUrl())}
        />
      </div>
      <div ref={treeRef} role="tree" aria-label="Project files" onKeyDown={handleKeyDown} onFocus={handleFocus} {...stylex.props(styles.tree)}>
        {rows.map((r) =>
          r.kind === "empty" ? (
            <div key={`${r.dir}/`} role="none" {...stylex.props(styles.row, indent(r.depth))}>
              <span {...stylex.props(styles.twist)} />
              <Text type="supporting" color="disabled">
                {r.dir === "nodes" ? "No nodes yet." : r.dir === "views" ? "No views yet." : "Empty."}
              </Text>
            </div>
          ) : r.entry.type === "dir" ? (
            <DirRow key={r.entry.path} dir={r.entry} depth={r.depth} ctx={ctx} />
          ) : (
            <FileRow key={r.entry.path} file={r.entry} depth={r.depth} ctx={ctx} />
          ),
        )}
        {filter.trim() && !tree.length && (
          <Text type="supporting" color="secondary" xstyle={styles.note}>
            No file path contains “{filter.trim()}”.
          </Text>
        )}
        {!project && (
          <Text type="supporting" color="secondary" xstyle={styles.note}>
            Loading…
          </Text>
        )}
      </div>
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

/** One row of the tree: a folder or a file, or the note inside a folder that holds nothing. */
type TreeRow = { kind: "entry"; entry: TreeEntry; depth: number } | { kind: "empty"; dir: string; depth: number };

function visibleRows(entries: TreeEntry[], folded: Set<string> | null, depth = 0): TreeRow[] {
  return entries.flatMap((entry): TreeRow[] => {
    if (entry.type === "file" || folded?.has(entry.path)) return [{ kind: "entry", entry, depth }];
    const inside: TreeRow[] = entry.children.length ? visibleRows(entry.children, folded, depth + 1) : [{ kind: "empty", dir: entry.path, depth: depth + 1 }];
    return [{ kind: "entry", entry, depth }, ...inside];
  });
}

function FileRow({ file, depth, ctx }: { file: TreeFile; depth: number; ctx: TreeCtx }) {
  const r = file.ref;
  const node = r.kind === "node" ? ctx.nodes.get(r.node) : undefined;
  const helper = r.kind === "lib" ? ctx.helpers.get(r.path) : undefined;
  const on = ctx.current === file.path;
  const dirty = file.path in helperDrafts;
  const where = r.kind === "node" ? "on the graph" : r.kind === "view" ? "in the view's Code tab" : "in the editor";
  const about = [
    node && `${node.name} · ${node.state}${node.reason ? ` · ${node.reason}` : ""}`,
    node?.parse_error,
    helper && (helper.used_by.length ? `Used by ${helper.used_by.join(", ")}` : "No node imports it"),
    dirty && "Unsaved changes",
    `Open ${file.path} ${where}`,
  ];
  return (
    <div
      role="treeitem"
      aria-level={depth + 1}
      aria-selected={on}
      data-tree-id={file.path}
      tabIndex={-1}
      title={about.filter(Boolean).join("\n")}
      onClick={() => ctx.open(r)}
      {...stylex.props(styles.row, styles.rowLive, on && styles.rowOn, indent(depth))}
    >
      <span {...stylex.props(styles.twist)} />
      <span {...stylex.props(styles.sign)}>{node ? <StateDot state={node.state} /> : <IconFile />}</span>
      <Text type="code" maxLines={1} hasTruncateTooltip={false} xstyle={styles.name}>
        {file.name}
      </Text>
      {dirty && <StatusDot variant="accent" label="Unsaved changes" />}
      {node?.parse_error && <Badge variant="error" label="error" />}
      {r.kind === "view" && r.entry && (
        <Text type="supporting" color="secondary">
          entry
        </Text>
      )}
      {helper && (
        <Text type="supporting" color="secondary" hasTabularNumbers>
          {helper.used_by.length} use{helper.used_by.length === 1 ? "" : "s"}
        </Text>
      )}
    </div>
  );
}

function DirRow({ dir, depth, ctx }: { dir: TreeDir; depth: number; ctx: TreeCtx }) {
  const expanded = !ctx.folded?.has(dir.path);
  const view = dir.view ? ctx.views.get(dir.view) : undefined;
  return (
    <div
      role="treeitem"
      aria-level={depth + 1}
      aria-expanded={expanded}
      data-tree-id={dir.path}
      tabIndex={-1}
      title={view ? `${view.title || view.name} · ${view.state}${view.reason ? ` · ${view.reason}` : ""}` : `${dir.path}/`}
      onClick={() => ctx.folded && ctx.toggle(dir.path)}
      {...stylex.props(styles.row, styles.rowLive, indent(depth))}
    >
      <span {...stylex.props(styles.twist, expanded && styles.twistOpen)}>
        <IconChevronRight size={10} />
      </span>
      <span {...stylex.props(styles.sign)}>{view ? <StateDot state={view.state} /> : <IconFolder />}</span>
      <Text type="code" maxLines={1} hasTruncateTooltip={false} xstyle={styles.name}>
        {dir.name}/
      </Text>
      <Text type="supporting" color="secondary" hasTabularNumbers>
        {countFiles(dir)}
      </Text>
    </div>
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
    <div {...stylex.props(styles.helper)}>
      {/* The file and what is done to it as a whole; under it who imports it, then its two faces. */}
      <div {...stylex.props(styles.head)}>
        <div {...stylex.props(styles.titleRow)}>
          <span {...stylex.props(styles.sign)}>
            <IconFile />
          </span>
          <Heading level={4} accessibilityLevel={2} maxLines={1} xstyle={styles.title}>
            {path}
          </Heading>
          <Text type="supporting" color="secondary" xstyle={styles.kind}>
            helper
          </Text>
          <span {...stylex.props(styles.grow)} />
          {dirty && (
            <Button size="sm" variant="ghost" label="Discard" tooltip="Throw away the unsaved edits to this file" isDisabled={busy} onClick={() => setDraft(null)} />
          )}
          {canEdit && (
            <Button
              size="sm"
              variant={dirty ? "primary" : "secondary"}
              label="Save"
              tooltip={dirty ? `Save this file (${MOD}S)` : "Nothing to save"}
              isDisabled={!dirty}
              isLoading={busy}
              onClick={save}
            />
          )}
          <IconButton size="sm" variant="ghost" label="Close the helper" tooltip="Close and go back to the graph" icon={<IconClose />} onClick={props.onClose} />
        </div>
        <div {...stylex.props(styles.usedRow)}>
          {file?.used_by.length ? (
            <>
              <Text type="supporting" color="secondary">
                Used by
              </Text>
              {file.used_by.map((d) => (
                <Token
                  key={d}
                  size="sm"
                  label={d}
                  icon={<StateDot state={state(d) ?? "error"} />}
                  description={`Open ${d} on the graph`}
                  isDisabled={!state(d)}
                  onClick={() => props.onOpenNode(d)}
                />
              ))}
            </>
          ) : (
            <Text type="supporting" color="secondary">
              {file ? "No node imports it" : "…"}
            </Text>
          )}
        </div>
        <TabList value={tab} onChange={(t) => setTab(t as "code" | "history")} aria-label={`${path}: its code and its history`}>
          <Tab value="code" label="Code" endContent={dirty ? <StatusDot variant="accent" label="Unsaved changes" /> : undefined} />
          <Tab value="history" label="History" />
        </TabList>
      </div>
      {tab === "code" ? (
        <div
          {...stylex.props(styles.code)}
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "s") {
              e.preventDefault();
              save();
            }
          }}
        >
          {err && <Banner status="error" container="section" title={err} />}
          {warn && <Banner status="warning" container="section" title={`Saved, but ${warn}`} />}
          {saved === null && !err ? (
            <div {...stylex.props(styles.placeholder)}>
              <Text color="secondary">Loading…</Text>
            </div>
          ) : (
            <CodeEditor
              value={text}
              onChange={(t) => setDraft(t === saved ? null : t)}
              editable={canEdit}
              readOnly={!canEdit}
              dark={props.dark}
              extensions={language(path)}
            />
          )}
          <div {...stylex.props(styles.status)}>
            <Text type="supporting" color="secondary" hasTabularNumbers>
              {text.split("\n").length} lines
            </Text>
            <span {...stylex.props(styles.grow)} />
            <Text type="supporting" color="secondary">
              {!canEdit ? "read-only" : dirty ? `unsaved · ${MOD}S save` : "saved"}
            </Text>
          </div>
        </div>
      ) : (
        <HistoryBrowser filter={{}} tick={project} dark={props.dark} layout="side" path={path} />
      )}
    </div>
  );
}

// A level of the tree is one step in: `--spacing-4`, as wide as the twist and its gap.
const depths = stylex.create({
  d0: { paddingInlineStart: spacingVars["--spacing-1"] },
  d1: { paddingInlineStart: `calc(${spacingVars["--spacing-1"]} + ${spacingVars["--spacing-4"]})` },
  d2: { paddingInlineStart: `calc(${spacingVars["--spacing-1"]} + 2 * ${spacingVars["--spacing-4"]})` },
  d3: { paddingInlineStart: `calc(${spacingVars["--spacing-1"]} + 3 * ${spacingVars["--spacing-4"]})` },
  d4: { paddingInlineStart: `calc(${spacingVars["--spacing-1"]} + 4 * ${spacingVars["--spacing-4"]})` },
  d5: { paddingInlineStart: `calc(${spacingVars["--spacing-1"]} + 5 * ${spacingVars["--spacing-4"]})` },
  d6: { paddingInlineStart: `calc(${spacingVars["--spacing-1"]} + 6 * ${spacingVars["--spacing-4"]})` },
});
const byDepth = [depths.d0, depths.d1, depths.d2, depths.d3, depths.d4, depths.d5, depths.d6];
/** A row's indent. Folders nested deeper than the steps there are stay on the last one. */
const indent = (depth: number) => byDepth[Math.min(depth, byDepth.length - 1)];

const styles = stylex.create({
  // The Files tab: the filter stays put, the tree under it scrolls.
  files: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: 0, minHeight: 0 },
  filesBar: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-1"],
    flexShrink: 0,
    paddingBlock: spacingVars["--spacing-2"],
    paddingInline: spacingVars["--spacing-2"],
  },
  grow: { flexGrow: 1, minWidth: 0 },
  tree: {
    display: "flex",
    flexDirection: "column",
    flexGrow: 1,
    minHeight: 0,
    overflowY: "auto",
    paddingInline: spacingVars["--spacing-2"],
    paddingBottom: spacingVars["--spacing-3"],
  },
  row: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-1-5"],
    flexShrink: 0,
    minHeight: sizeVars["--size-element-sm"],
    paddingInlineEnd: spacingVars["--spacing-2"],
    borderRadius: radiusVars["--radius-inner"],
  },
  // A row that is pressed: a folder or a file.
  rowLive: {
    cursor: "pointer",
    userSelect: "none",
    color: colorVars["--color-text-primary"],
    backgroundColor: { default: "transparent", ":hover": colorVars["--color-background-muted"] },
    outlineStyle: { default: "none", ":focus-visible": focusVars["--focus-outline-style"] },
    outlineWidth: focusVars["--focus-outline-width"],
    outlineColor: focusVars["--focus-outline-color"],
    outlineOffset: `calc(-1 * ${focusVars["--focus-outline-width"]})`,
  },
  // The open file: Astryx's ground for a selected row, and a rule in the accent down its leading edge.
  rowOn: {
    backgroundColor: colorVars["--color-accent-muted"],
    boxShadow: `inset ${spacingVars["--spacing-0-5"]} 0 0 ${colorVars["--color-accent"]}`,
  },
  // The chevron's column: files keep it empty, so names line up under their folder's.
  twist: {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    width: spacingVars["--spacing-3"],
    color: colorVars["--color-icon-secondary"],
    transitionProperty: "transform",
    transitionDuration: durationVars["--duration-fast"],
  },
  twistOpen: { transform: "rotate(90deg)" },
  // A file's or a folder's sign: an icon, or the state's dot of a node or a view.
  sign: {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    width: spacingVars["--spacing-4"],
    color: colorVars["--color-icon-secondary"],
  },
  name: { flexGrow: 1, minWidth: 0 },
  note: { display: "block", paddingBlock: spacingVars["--spacing-2"], paddingInline: spacingVars["--spacing-2"] },

  // A helper in the main area: its head, then the editor or the history filling the rest.
  helper: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: 0, minHeight: 0 },
  head: {
    display: "flex",
    flexDirection: "column",
    gap: spacingVars["--spacing-1"],
    flexShrink: 0,
    paddingTop: spacingVars["--spacing-2"],
    paddingInline: spacingVars["--spacing-4"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  titleRow: { display: "flex", alignItems: "center", gap: spacingVars["--spacing-2"], minWidth: 0 },
  title: { minWidth: 0 },
  kind: { flexShrink: 0 },
  usedRow: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    gap: spacingVars["--spacing-1-5"],
    minHeight: sizeVars["--size-element-sm"],
  },
  code: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: 0, minHeight: 0 },
  placeholder: { display: "grid", placeItems: "center", flexGrow: 1, minHeight: 0, padding: spacingVars["--spacing-6"] },
  status: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    flexShrink: 0,
    paddingBlock: spacingVars["--spacing-1"],
    paddingInline: spacingVars["--spacing-4"],
    borderTopWidth: "1px",
    borderTopStyle: "solid",
    borderTopColor: colorVars["--color-border"],
  },
});
