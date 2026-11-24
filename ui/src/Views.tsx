import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, sizeVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import type { ISODateString } from "@astryxdesign/core/Calendar";
import { Code } from "@astryxdesign/core/Code";
import { DateInput } from "@astryxdesign/core/DateInput";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Link } from "@astryxdesign/core/Link";
import { List, ListItem } from "@astryxdesign/core/List";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Spinner } from "@astryxdesign/core/Spinner";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Switch } from "@astryxdesign/core/Switch";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { TextInput, type TextInputProps } from "@astryxdesign/core/TextInput";
import { Token } from "@astryxdesign/core/Token";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { VStack } from "@astryxdesign/core/VStack";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import { api, HUB_PROJECT, type GraphNode, type Project, type ProjectParam, type ProjectView } from "./api";
import { appUrl } from "./appMode";
import { fileOrder, missingNpmPackage } from "./views/files";
import { ViewBridge, frameDocument, type FrameStatus } from "./views/host";
import type { Params, ParamsState } from "./views/protocol";
import { IconChevronRight, IconClose, IconExpand, IconPlay, MOD } from "./icons";
import { language } from "./lang";
import { HistoryBrowser } from "./History";
import { useAccess } from "./Access";
import { codeGroup, DownloadMenu, viewOutputGroups } from "./Export";
import { CodeEditor } from "./ui/CodeEditor";
import { StateDot, statusOf, statusText } from "./ui/state";

/** What the main pane shows for the selected view: the rendered view, its files or their history. */
type Pane = "view" | "code" | "history";
/** Unsaved edits: view name → file path → text. */
type Drafts = Record<string, Record<string, string>>;
// Kept outside the component, so leaving Views mode for the graph and coming
// back keeps unsaved edits and the open pane.
let keptDrafts: Drafts = {};
let keptPane: Pane = "view";
let keptView: string | null = null;
/** The newest open request (see `open` on Views) already shown. */
let shownOpen = 0;

/** Show one view, rendered or with one of its files in the Code pane; `seq` tells a new request from one already shown. */
export interface OpenView {
  view: string;
  file?: string;
  seq: number;
}

/** Overrides → the parameter set a view sees (resolved values + declarations). */
export function paramsState(params: ProjectParam[]): ParamsState {
  return { params: Object.fromEntries(params.map((p) => [p.name, p.value])), specs: params };
}

const VIEW_STATE_LABEL: Record<ProjectView["state"], string> = {
  ready: "ready",
  missing: "missing",
  stale: "stale",
  error: "error",
};

/**
 * Views mode: the project's views (custom UI under views/) next to the
 * canvas. A view renders in a sandboxed iframe with the parameter set chosen
 * here; it only reads outputs, and opening it re-runs only the live nodes
 * it depends on (useLiveRefresh).
 */
export function Views(props: {
  project: Project | null;
  nodes: GraphNode[];
  dark: boolean;
  onOpenNode: (name: string) => void;
  /** A view (and file) picked elsewhere, in the side panel's Files tab. */
  open?: OpenView | null;
  /** Told which view is shown. */
  onShown?: (view: string | null) => void;
}) {
  const { project, dark, onOpenNode, open, onShown } = props;
  const [overrides, setOverrides] = useState<Params>({});
  const [at, setAt] = useState<Project | null>(null);
  const [paramErr, setParamErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(keptView);
  const [pane, setPane] = useState<Pane>(keptPane);
  useEffect(() => void (keptView = selected), [selected]);
  useEffect(() => {
    if (!open || open.seq <= shownOpen) return;
    shownOpen = open.seq;
    setSelected(open.view);
    if (open.file) setOpenFiles((f) => ({ ...f, [open.view]: open.file! }));
    setPane(open.file ? "code" : "view");
  }, [open]);
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
  useEffect(() => onShown?.(view?.name ?? null), [onShown, view?.name]);
  const refreshing = useLiveRefresh(at ? view : null, overrides);

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

  const narrow = useMediaQuery("(max-width: 1100px)");
  // Nothing to list: the main area has the whole width (and says what a view is).
  const alone = views.length === 0 && (narrow || at !== null);
  const unsaved = (v: ProjectView) => (drafts[v.name] ? <StatusDot variant="accent" label="Unsaved changes" tooltip="Unsaved changes" /> : undefined);

  return (
    <div {...stylex.props(styles.views, alone && styles.viewsAlone)}>
      {alone ? null : narrow ? (
        <TabList value={view?.name ?? ""} onChange={setSelected} aria-label="Views" hasDivider xstyle={styles.strip}>
          {views.map((v) => (
            <Tab key={v.name} value={v.name} label={v.title || v.name} icon={<StateDot state={v.state} />} endContent={unsaved(v)} />
          ))}
        </TabList>
      ) : (
        <aside {...stylex.props(styles.list)} aria-label="Views">
          <List
            density="compact"
            header={
              <Heading level={5} accessibilityLevel={2} color="secondary" xstyle={styles.listTitle}>
                Views
              </Heading>
            }
          >
            {views.map((v) => (
              <ListItem
                key={v.name}
                isSelected={view?.name === v.name}
                onClick={() => setSelected(v.name)}
                align="first-line"
                startContent={<StateDot state={v.state} />}
                label={
                  <HStack gap={1.5} vAlign="center">
                    <Text weight="medium" maxLines={1}>
                      {v.title || v.name}
                    </Text>
                    {unsaved(v)}
                  </HStack>
                }
                description={
                  v.reason || v.description ? (
                    <Text type="supporting" color="secondary" maxLines={2}>
                      {v.reason || v.description}
                    </Text>
                  ) : undefined
                }
                endContent={
                  <Text type="supporting" weight="medium" xstyle={statusText[statusOf(v.state)]}>
                    {VIEW_STATE_LABEL[v.state]}
                  </Text>
                }
              />
            ))}
          </List>
        </aside>
      )}
      <section {...stylex.props(styles.main)}>
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
            refreshing={refreshing}
          />
        ) : at ? (
          <div {...stylex.props(styles.placeholder)}>
            <VStack gap={2} maxWidth={520}>
              <Heading level={3} accessibilityLevel={2}>
                No views yet
              </Heading>
              <Text color="secondary">
                A view is a folder <Code>views/&lt;name&gt;/</Code> with a <Code>view.json</Code> (<Code>{'{"title", "reads": [nodes]}'}</Code>) and an{" "}
                <Code>App.tsx</Code> whose default export is a React component. Ask the agent to write one.
              </Text>
            </VStack>
          </div>
        ) : (
          <Placeholder>Loading…</Placeholder>
        )}
      </section>
    </div>
  );
}

/** What a pane says while it has nothing to show. */
function Placeholder(props: { children: ReactNode }) {
  return (
    <div {...stylex.props(styles.placeholder)}>
      <Text color="secondary">{props.children}</Text>
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
  /** Read nodes being re-run (useLiveRefresh). */
  refreshing: string[];
}) {
  const { view, params, dark, pane } = props;
  const { canEdit } = useAccess();
  const [status, setStatus] = useState<FrameStatus>({ phase: "loading" });
  const [errors, setErrors] = useState<FrameError[]>([]);
  const [runErr, setRunErr] = useState<string | null>(null);
  const known = new Set(props.nodes.map((n) => n.name));
  const outputs = new Map(view.outputs.map((o) => [o.node, o]));
  // Not the ones already running (a live refresh, say).
  const toRun = view.outputs.filter((o) => (o.state !== "fresh" || !o.artifact_id) && !busy(o.state)).map((o) => o.node);

  const onStatus = useCallback((s: FrameStatus) => {
    setStatus(s);
    if ("error" in s) setErrors((es) => [...es.slice(-19), s]);
  }, []);
  // A new version of the view's files starts over with a fresh frame.
  useEffect(() => {
    setErrors([]);
    setStatus({ phase: "loading" });
  }, [view.hash]);

  /** Add an npm package the view imports to package.json; the new list rebuilds the view. */
  const addPackage = async (name: string) => {
    await api.npmEdit({ add: [name] });
  };

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

  const title = view.title || view.name;
  const panel = useId();
  const historyTab = useRef<HTMLButtonElement>(null);
  const failing = "error" in status;

  return (
    <div {...stylex.props(styles.pane)}>
      <header {...stylex.props(styles.head)}>
        <div {...stylex.props(styles.titleRow)}>
          <Heading level={3} accessibilityLevel={2} maxLines={1} xstyle={styles.title}>
            {title}
          </Heading>
          <HStack gap={1.5} vAlign="center" xstyle={styles.fixed}>
            <StateDot state={view.state} />
            <Text weight="medium" xstyle={statusText[statusOf(view.state)]}>
              {view.state}
            </Text>
          </HStack>
          {/* What the frame is doing. A view that can't be read has no frame. */}
          {view.state !== "error" && (
            <Text type="supporting" color="secondary" xstyle={[styles.fixed, failing && statusText.error]}>
              {failing ? `${status.phase} error` : status.phase}
            </Text>
          )}
          <Refreshing view={view} nodes={props.refreshing} xstyle={styles.fixed} />
          <span {...stylex.props(styles.grow)} />
          {view.state !== "error" && (
            <IconButton
              size="sm"
              variant="ghost"
              label="Open as an app"
              tooltip="Open this view full screen, without the workbench, in a new tab"
              icon={<IconExpand />}
              onClick={() => window.open(appUrl(location.origin, HUB_PROJECT, view.name, props.overrides), "_blank", "noopener")}
            />
          )}
          <DownloadMenu
            small
            title={`Download what ${title} reads, or its code`}
            groups={[...view.outputs.flatMap((o) => viewOutputGroups(view.name, o, params.params)), codeGroup({ view: view.name, title: "View code" })]}
          />
        </div>
        {view.state !== "ready" && view.reason && <Text xstyle={statusText[statusOf(view.state)]}>{view.reason}</Text>}
        {view.description && <Text color="secondary">{view.description}</Text>}
        {(view.reads.length > 0 || props.specs.length > 0) && (
          <div {...stylex.props(styles.facts)}>
            {view.reads.length > 0 && (
              <>
                <Tooltip content="The nodes this view reads (view.json reads)">
                  <Text type="supporting" color="secondary">
                    Reads
                  </Text>
                </Tooltip>
                <HStack gap={1.5} wrap="wrap" vAlign="center">
                  {view.reads.map((n) => {
                    const o = outputs.get(n);
                    const here = known.has(n);
                    // What is wrong with it is said on the chip; a tooltip never opens on one that can't be pressed.
                    const wrong = !here ? "no such node" : o && !o.artifact_id ? "no output" : null;
                    return (
                      <Tooltip key={n} content={o?.artifact_id ? `${o.state} · ${o.kind}` : "no output for these parameters"} isEnabled={here}>
                        <Token
                          size="sm"
                          label={n}
                          icon={<StateDot state={o ? o.state : "error"} />}
                          isDisabled={!here}
                          onClick={() => here && props.onOpenNode(n)}
                          endContent={
                            wrong ? (
                              <Text type="supporting" color="secondary">
                                {wrong}
                              </Text>
                            ) : undefined
                          }
                        />
                      </Tooltip>
                    );
                  })}
                  {canEdit && toRun.length > 0 && (
                    <Button
                      size="sm"
                      variant="primary"
                      icon={<IconPlay />}
                      label={`Run ${toRun.length === 1 ? toRun[0] : `${toRun.length} nodes`}`}
                      tooltip="Run the read nodes that are missing or stale, for these parameters"
                      clickAction={runReads}
                    />
                  )}
                </HStack>
              </>
            )}
            {props.specs.length > 0 && (
              <>
                <Text type="supporting" color="secondary">
                  Parameters
                </Text>
                <ParamBar specs={props.specs} overrides={props.overrides} onChange={props.onParams} />
              </>
            )}
          </div>
        )}
        {(props.paramErr || runErr) && <Banner status="error" title={props.paramErr ?? runErr} />}
        <TabList
          value={pane}
          onChange={(p) => props.onPane(p as Pane)}
          role="tablist"
          aria-label={`${view.name}: the rendered view, its code and its history`}
          xstyle={styles.tabs}
        >
          <Tab value="view" label="View" panelId={panel} />
          <Tab
            value="code"
            label="Code"
            panelId={panel}
            endContent={
              <HStack gap={1.5} vAlign="center">
                {Object.keys(props.drafts).length > 0 && <StatusDot variant="accent" label="Unsaved changes" />}
                <Badge label={view.files.length} />
              </HStack>
            }
          />
          <Tab ref={historyTab} value="history" label="History" panelId={panel} />
        </TabList>
        <Tooltip anchorRef={historyTab} placement="below" content="Every saved version of this view's files, deleted ones too" />
      </header>
      <div {...stylex.props(styles.panel)} id={panel} role="tabpanel">
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
          <div {...stylex.props(styles.history)}>
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
            <div {...stylex.props(styles.placeholder)}>
              <VStack gap={3} hAlign="center">
                <Text color="secondary">Fix view.json to render this view.</Text>
                <Button label="Open view.json" onClick={() => openFile("view.json")} />
              </VStack>
            </div>
          )
        ) : (
          // Stays mounted while the code is open, so saving a file rebuilds it and
          // its compile and runtime errors show up under the editor.
          <ViewFrame
            key={view.hash}
            view={view}
            params={params}
            dark={dark}
            hidden={pane !== "view"}
            refreshing={props.refreshing}
            onParams={props.onParams}
            onStatus={onStatus}
          />
        )}
      </div>
      {errors.length > 0 && (
        <Banner
          status="error"
          container="section"
          title={`${errors.length} error${errors.length === 1 ? "" : "s"} from the view`}
          endContent={<Button size="sm" variant="ghost" label="Clear" onClick={() => setErrors([])} />}
          collapsible={{ defaultIsOpen: true }}
          xstyle={styles.errors}
        >
          <div {...stylex.props(styles.errorList)}>
            {errors
              .slice()
              .reverse()
              .map((e) => (
                <ViewError key={errorId(e)} error={e} onOpenFile={openFile} onAddPackage={canEdit ? addPackage : undefined} />
              ))}
          </div>
        </Banner>
      )}
    </div>
  );
}

export type FrameError = Extract<FrameStatus, { error: string }>;
// An error keeps its row (and whether its stack is open) as newer ones arrive above it.
const errorIds = new WeakMap<FrameError, number>();
let lastErrorId = 0;
function errorId(e: FrameError): number {
  if (!errorIds.has(e)) errorIds.set(e, ++lastErrorId);
  return errorIds.get(e)!;
}

/** One error from the view: where it happened, the file to open, the message, and its stack behind the arrow. */
function ViewError(props: { error: FrameError; onOpenFile: (path: string) => void; onAddPackage?: (name: string) => Promise<void> }) {
  const e = props.error;
  const [open, setOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [addErr, setAddErr] = useState<string | null>(null);
  const missing = props.onAddPackage ? missingNpmPackage(e.error) : null;
  const add = async () => {
    setAdding(true);
    setAddErr(null);
    try {
      await props.onAddPackage!(missing!);
    } catch (err) {
      setAddErr((err as Error).message);
      setAdding(false);
    }
  };
  return (
    <div {...stylex.props(styles.error)}>
      <div {...stylex.props(styles.errorLine)}>
        {e.stack ? (
          <IconButton
            size="sm"
            variant="ghost"
            label={open ? "Hide the stack" : "Show the stack"}
            tooltip={open ? "Hide the stack" : "Show the stack"}
            aria-expanded={open}
            icon={
              <span {...stylex.props(open && styles.down)}>
                <IconChevronRight />
              </span>
            }
            onClick={() => setOpen(!open)}
          />
        ) : (
          <span {...stylex.props(styles.noStack)} />
        )}
        <Text type="supporting" weight="medium" xstyle={[styles.fixed, statusText.error]}>
          {e.phase}
        </Text>
        {e.file && (
          <Link type="code" tooltip={`Open ${e.file}`} onClick={() => props.onOpenFile(e.file!)}>
            {e.file}
          </Link>
        )}
        <Text type="code" xstyle={styles.errorText}>
          {e.error}
          {addErr && `
${addErr}`}
        </Text>
        {missing && (
          <Button
            size="sm"
            label={adding ? "Adding…" : `Add ${missing}`}
            isDisabled={adding}
            xstyle={styles.fixed}
            tooltip={`Add ${missing} to the project's package.json; the view reloads with it`}
            onClick={add}
          />
        )}
      </div>
      {open && e.stack && (
        <Text type="code" color="secondary" display="block" xstyle={styles.stack}>
          {e.stack}
        </Text>
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
  const { canEdit } = useAccess();
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
    if (!canEdit) return;
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

  const narrow = useMediaQuery("(max-width: 1100px)");
  const [discarding, setDiscarding] = useState(false);
  const unsavedDot = (f: string) => (f in drafts ? <StatusDot variant="accent" label="Unsaved changes" tooltip="Unsaved changes" /> : undefined);

  return (
    <div {...stylex.props(styles.code)}>
      {narrow ? (
        <TabList value={current ?? ""} onChange={props.onFile} aria-label={`Files of ${view.name}`} size="sm" hasDivider xstyle={styles.strip}>
          {files.map((f) => (
            <Tab key={f} value={f} label={f} endContent={unsavedDot(f)} />
          ))}
        </TabList>
      ) : (
        <nav {...stylex.props(styles.files)} aria-label={`Files of ${view.name}`}>
          <List density="compact">
            {files.map((f) => (
              <ListItem
                key={f}
                isSelected={f === current}
                onClick={() => props.onFile(f)}
                label={
                  <Text type="code" color={f === current ? "primary" : "secondary"} maxLines={1}>
                    {f}
                  </Text>
                }
                endContent={unsavedDot(f)}
              />
            ))}
          </List>
        </nav>
      )}
      <div
        {...stylex.props(styles.editor)}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === "s") {
            e.preventDefault();
            if (dirty) save([current!]);
          }
        }}
      >
        {err && <Banner status="error" container="section" title={err} />}
        {current ? (
          <CodeEditor
            key={current}
            value={text}
            onChange={(t) => onDraft(current, t === saved?.[current] ? null : t)}
            editable={canEdit}
            readOnly={!canEdit}
            dark={props.dark}
            extensions={language(current)}
          />
        ) : (
          <Placeholder>{saved ? "This view has no files." : "Loading…"}</Placeholder>
        )}
        <div {...stylex.props(styles.codeBar)}>
          <Text type="code" color="secondary" maxLines={1} xstyle={styles.codePath}>
            {current ? `${view.path}/${current}` : view.path}
          </Text>
          <HStack gap={2} vAlign="center" xstyle={styles.fixed}>
            <Text type="supporting" color="secondary">
              {dirty ? `unsaved · ${MOD}S save` : "saved"}
            </Text>
            <Button
              size="sm"
              variant="ghost"
              label="History"
              tooltip={current ? `Every saved version of ${current}` : "Every saved version of this view's files"}
              onClick={() => {
                if (current) props.onFile(current);
                props.onHistory();
              }}
            />
            {dirty && (
              <Button size="sm" variant="ghost" label="Discard" tooltip="Throw away the unsaved edits to this file" isDisabled={busy} onClick={() => setDiscarding(true)} />
            )}
            {canEdit && unsaved.length > 1 && <Button size="sm" label={`Save all (${unsaved.length})`} isDisabled={busy} onClick={() => save(unsaved)} />}
            {canEdit ? (
              <Button size="sm" variant={dirty ? "primary" : "secondary"} label="Save" isDisabled={!dirty || busy} onClick={() => save([current!])} />
            ) : (
              <Text type="supporting" color="secondary">
                read-only
              </Text>
            )}
          </HStack>
        </div>
      </div>
      {discarding && dirty && (
        <AlertDialog
          isOpen
          onOpenChange={setDiscarding}
          title={`Discard the edits to ${current}?`}
          description="The unsaved edits to this file are thrown away. The file stays as it was last saved."
          actionLabel="Discard"
          onAction={() => {
            onDraft(current, null);
            setDiscarding(false);
          }}
        />
      )}
    </div>
  );
}

/** The run parameters, one control each. A value other than the default is marked, and has a way back to it. */
export function ParamBar(props: { specs: ProjectParam[]; overrides: Params; onChange: (p: Params) => Promise<ParamsState> }) {
  return (
    <div {...stylex.props(styles.params)}>
      {props.specs.map((p) => (
        <Param key={p.name} spec={p} custom={Object.prototype.hasOwnProperty.call(props.overrides, p.name)} onChange={props.onChange} />
      ))}
    </div>
  );
}

function Param(props: { spec: ProjectParam; custom: boolean; onChange: (p: Params) => Promise<ParamsState> }) {
  const { spec: p, custom } = props;
  const v = p.value;
  const set = (value: unknown) => props.onChange({ [p.name]: value }).catch(() => {});
  const field = { label: p.name, isLabelHidden: true, size: "sm", controlXstyle: custom && styles.custom } as const;
  return (
    <span {...stylex.props(styles.param)}>
      <Tooltip content={p.description} isEnabled={!!p.description}>
        <Text type="code" color={custom ? "accent" : "secondary"}>
          {p.name}
        </Text>
      </Tooltip>
      {p.type === "boolean" ? (
        <Switch size="sm" label={p.name} isLabelHidden value={!!v} onChange={(on) => set(on)} />
      ) : p.type === "date" ? (
        <DateInput
          {...field}
          format="system_date"
          width={184}
          value={typeof v === "string" && v ? (v as ISODateString) : undefined}
          onChange={(d) => d !== v && set(d ?? null)}
        />
      ) : p.type === "number" ? (
        <NumberInput {...field} width={96} isWheelEnabled={false} value={v == null ? null : Number(v)} onChange={(n) => n !== v && set(n)} />
      ) : (
        <TextParam key={String(v)} field={field} value={v == null ? "" : String(v)} onCommit={set} />
      )}
      {custom && (
        <IconButton
          size="sm"
          variant="ghost"
          icon={<IconClose />}
          label={`Reset ${p.name} to its default`}
          tooltip={`Back to the default (${String(p.default)})`}
          onClick={() => set(null)}
        />
      )}
    </span>
  );
}

/** A text parameter: typed freely, and set when the field is left or Enter is pressed. */
function TextParam(props: { field: Pick<TextInputProps, "label" | "isLabelHidden" | "size" | "controlXstyle">; value: string; onCommit: (text: string) => void }) {
  const [text, setText] = useState(props.value);
  return (
    <span {...stylex.props(styles.contents)} onBlur={() => text !== props.value && props.onCommit(text)}>
      <TextInput {...props.field} width={150} value={text} onChange={setText} onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()} />
    </span>
  );
}

/**
 * Live nodes (`live=True`): opening a view, or showing it at other
 * parameters, re-runs the live nodes it depends on. The view renders what is
 * stored at once and updates when the new outputs land. Returns the read
 * nodes being re-run (by that or by any other run), for the indicator and
 * the view's `useRefreshing()`; empty when nothing is on its way.
 * `params` null: not known yet, ask nothing.
 */
export function useLiveRefresh(view: ProjectView | null, params: Params | null): string[] {
  const name = view?.name ?? null;
  const live = (view?.live?.length ?? 0) > 0;
  const key = params === null ? null : JSON.stringify(params);
  // "asking": the request is out. A view: the answer came while that
  // snapshot was shown, which predates the runs; the next one shows them.
  const [waiting, setWaiting] = useState<"asking" | ProjectView | null>(null);
  const shown = useRef(view);
  shown.current = view;
  useEffect(() => {
    if (!name || !live || key === null) return;
    let on = true;
    setWaiting("asking");
    api.refreshView(name, JSON.parse(key) as Params).then(
      (r) => on && setWaiting(r.started.length ? shown.current : null),
      (e: Error) => {
        if (!on) return;
        setWaiting(null);
        console.warn(`Could not refresh the live nodes of ${name}: ${e.message}`);
      },
    );
    return () => {
      on = false;
      setWaiting(null);
    };
  }, [name, live, key]);
  useEffect(() => {
    if (waiting !== null && waiting !== "asking" && waiting !== view) setWaiting(null);
  }, [view, waiting]);
  return useMemo(() => {
    if (!view) return [];
    if (waiting === "asking" || waiting === view) return view.reads;
    return view.outputs.filter((o) => busy(o.state)).map((o) => o.node);
  }, [view, waiting]);
}

/** A read node being re-run: its output is on its way. */
export const busy = (state: string) => state === "running" || state === "queued";

/** "Refreshing", while what a view reads is being re-run and it shows the last output. */
export function Refreshing(props: { view: ProjectView; nodes: string[]; xstyle?: stylex.StyleXStyles }) {
  if (!props.nodes.length) return null;
  const live = props.view.live ?? [];
  const tip = live.length
    ? `Showing the last output while the live data is refreshed (${live.join(", ")}); the view updates when it lands`
    : `Showing the last output while ${props.nodes.join(", ")} re-run${props.nodes.length === 1 ? "s" : ""}; the view updates when it lands`;
  return (
    <Tooltip content={tip}>
      <HStack gap={1.5} vAlign="center" xstyle={props.xstyle}>
        <Spinner size="sm" aria-label="Refreshing" />
        <Text type="supporting" color="secondary">
          Refreshing
        </Text>
      </HStack>
    </Tooltip>
  );
}

/** The sandboxed iframe and its bridge. Remounted when the view's files change. */
export function ViewFrame(props: {
  view: ProjectView;
  params: ParamsState;
  dark: boolean;
  /** Kept running but not shown (while the Code pane is open). */
  hidden: boolean;
  /** Read nodes being re-run, for the view's `useRefreshing()`. */
  refreshing?: string[];
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
    b.setRefreshing(p.refreshing ?? []);
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
  const refreshing = (props.refreshing ?? []).join();
  useEffect(() => bridge.current?.setRefreshing(refreshing ? refreshing.split(",") : []), [refreshing]);

  return (
    <iframe
      ref={ref}
      {...stylex.props(styles.frame, props.hidden && styles.hidden)}
      title={props.view.title || props.view.name}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      hidden={props.hidden}
    />
  );
}

/**
 * Views mode fills the cell under the project's bar.
 *
 *   > 1100px   the views 240 | the selected view
 *              its Code tab: the files 200 | the editor
 *   <= 1100px  one column: the views are a strip of tabs over the selected
 *              view, and so are the Code tab's files over the editor
 */
const NARROW = "@media (max-width: 1100px)";

const styles = stylex.create({
  views: {
    display: "grid",
    gridTemplateColumns: { default: "240px minmax(0, 1fr)", [NARROW]: "minmax(0, 1fr)" },
    gridTemplateRows: { default: "minmax(0, 1fr)", [NARROW]: "auto minmax(0, 1fr)" },
    minWidth: 0,
    minHeight: 0,
  },
  viewsAlone: { gridTemplateColumns: "minmax(0, 1fr)", gridTemplateRows: "minmax(0, 1fr)" },
  list: {
    borderInlineEndWidth: "1px",
    borderInlineEndStyle: "solid",
    borderInlineEndColor: colorVars["--color-border"],
    minHeight: 0,
    overflow: "auto",
    padding: spacingVars["--spacing-2"],
    backgroundColor: colorVars["--color-background-surface"],
  },
  listTitle: { paddingInline: spacingVars["--spacing-2"], paddingBlock: spacingVars["--spacing-1"] },
  // The views, or a view's files, where there is no room for a column of them.
  strip: { minWidth: 0, paddingInline: spacingVars["--spacing-1"], backgroundColor: colorVars["--color-background-surface"] },
  main: { display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0 },
  placeholder: {
    display: "grid",
    placeItems: "center",
    flexGrow: 1,
    minHeight: 0,
    padding: spacingVars["--spacing-6"],
    textAlign: "center",
  },
  pane: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: 0, minHeight: 0 },
  // The tabs sit on the head's bottom edge, so the head has no padding there.
  head: {
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
    display: "flex",
    flexDirection: "column",
    gap: spacingVars["--spacing-2"],
    flexShrink: 0,
    paddingTop: spacingVars["--spacing-3"],
    paddingInline: spacingVars["--spacing-4"],
    backgroundColor: colorVars["--color-background-surface"],
  },
  titleRow: { display: "flex", alignItems: "center", gap: spacingVars["--spacing-3"], minWidth: 0 },
  title: { minWidth: 0 },
  fixed: { flexShrink: 0, whiteSpace: "nowrap" },
  grow: { flexGrow: 1 },
  // What the view reads and the parameters it is shown with: the names on one edge, the controls on another.
  facts: {
    display: "grid",
    gridTemplateColumns: "max-content minmax(0, 1fr)",
    alignItems: "baseline",
    columnGap: spacingVars["--spacing-3"],
    rowGap: spacingVars["--spacing-2"],
  },
  // A tab's label, not its box, is on the head's edge.
  tabs: { minWidth: 0, marginInlineStart: `calc(${spacingVars["--spacing-3"]} * -1)` },
  panel: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: 0, minHeight: 0 },
  history: { display: "flex", flexDirection: "column", flexGrow: 1, minHeight: 0 },
  frame: {
    display: "block",
    flexGrow: 1,
    minHeight: 0,
    width: "100%",
    borderWidth: 0,
    backgroundColor: colorVars["--color-background-body"],
  },
  hidden: { display: "none" },

  params: { display: "flex", flexWrap: "wrap", alignItems: "center", columnGap: spacingVars["--spacing-4"], rowGap: spacingVars["--spacing-2"] },
  // A parameter's name, its control and the way back to its default stay together when the row wraps.
  param: { display: "inline-flex", alignItems: "center", gap: spacingVars["--spacing-1-5"], flexShrink: 0, whiteSpace: "nowrap" },
  // A parameter set to something other than its default.
  custom: { backgroundColor: colorVars["--color-accent-muted"], borderColor: colorVars["--color-accent"] },
  contents: { display: "contents" },

  code: {
    display: "grid",
    gridTemplateColumns: { default: "200px minmax(0, 1fr)", [NARROW]: "minmax(0, 1fr)" },
    gridTemplateRows: { default: "minmax(0, 1fr)", [NARROW]: "auto minmax(0, 1fr)" },
    flexGrow: 1,
    minHeight: 0,
  },
  files: {
    borderInlineEndWidth: "1px",
    borderInlineEndStyle: "solid",
    borderInlineEndColor: colorVars["--color-border"],
    minHeight: 0,
    overflow: "auto",
    padding: spacingVars["--spacing-2"],
    backgroundColor: colorVars["--color-background-surface"],
  },
  editor: { display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0 },
  codeBar: {
    borderTopWidth: "1px",
    borderTopStyle: "solid",
    borderTopColor: colorVars["--color-border"],
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    flexShrink: 0,
    paddingBlock: spacingVars["--spacing-1"],
    paddingInline: spacingVars["--spacing-3"],
    backgroundColor: colorVars["--color-background-surface"],
  },
  // The path gives way first; the state and the actions keep their words.
  codePath: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0 },

  // The errors drawer, under the pane: a long list scrolls, so the pane keeps most of the height.
  errors: { flexShrink: 0 },
  errorList: { maxHeight: "26vh", overflow: "auto" },
  error: {
    borderTopWidth: { default: "1px", ":first-child": 0 },
    borderTopStyle: "solid",
    borderTopColor: colorVars["--color-border"],
  },
  errorLine: { display: "flex", alignItems: "baseline", gap: spacingVars["--spacing-2"], paddingBlock: spacingVars["--spacing-1"] },
  // As wide as the arrow of an error that has a stack, so every row starts on one edge.
  noStack: { width: sizeVars["--size-element-sm"], flexShrink: 0 },
  down: { display: "block", transform: "rotate(90deg)" },
  errorText: { flexGrow: 1, minWidth: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere" },
  stack: {
    paddingBlockEnd: spacingVars["--spacing-2"],
    paddingInlineStart: spacingVars["--spacing-8"],
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
  },
});
