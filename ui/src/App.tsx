import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { ReactFlowProvider } from "@xyflow/react";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Divider } from "@astryxdesign/core/Divider";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import { access, api, apiUrl, HUB_PROJECT, type Access, type CodeRevision, type PackagesState, type Project } from "./api";
import { AccessContext, accessState, NoAccess, useAccess, ViewOnlyBanner } from "./Access";
import { Canvas } from "./Canvas";
import { Inspector, Overview } from "./Inspector";
import { SidePanel, SideRail, type AgentHandle, type SideTab } from "./AgentPanel";
import { HistoryBrowser, HistoryRail } from "./History";
import type { OpenView } from "./Views";
import { FileTree, HelperEditor } from "./Files";
import { codeGroup, DownloadMenu } from "./Export";
import type { FileRef } from "./tree";
import { SignedInAs } from "./Me";
import { HelpMenu } from "./Help";
import { useColorMode } from "./theme/colorMode";
import { Brand, Mark } from "./Brand";
import { IconClock, IconClose, IconMoon, IconPackage, IconPlay, IconPlus, IconShare, IconSun } from "./icons";
import { StateDot } from "./ui/state";
import { failed, Toasts } from "./toast";
import { appViewOf } from "./appMode";

// What is not on the first screen is fetched when it is opened: the Views
// mode and the dialogs.
const Views = lazy(() => import("./Views").then((m) => ({ default: m.Views })));
const ShareDialog = lazy(() => import("./Share").then((m) => ({ default: m.ShareDialog })));
const NewNodeDialog = lazy(() => import("./NewNodeDialog").then((m) => ({ default: m.NewNodeDialog })));
const PackagesDialog = lazy(() => import("./PackagesDialog").then((m) => ({ default: m.PackagesDialog })));
const AppView = lazy(() => import("./AppView"));

const embed = new URLSearchParams(location.search).has("embed");
/** `?app=<view>`: that one view as an app, without the workbench (AppView.tsx). */
const appView = appViewOf(location.search);

/** What the main area shows: the graph, the views, a shared helper, or the code's history. */
type Mode = "graph" | "views" | "helper" | "history";

/**
 * Where the address points on load. `#files/<path>` opens a file where it
 * lives: a node on the graph, a view file in its Code tab, a helper in the
 * editor; `#views/<name>` a view, rendered; `#history` the code's history.
 */
function fromHash(): { mode: Mode; node?: string; view?: OpenView; helper?: string } {
  const h = location.hash;
  if (h === "#views") return { mode: "views" };
  const shown = /^#views\/([a-z_][a-z0-9_]*)$/.exec(h);
  if (shown) return { mode: "views", view: { view: shown[1], seq: 1 } };
  if (h === "#history") return { mode: "history" };
  let path = "";
  try {
    path = decodeURI(/^#files\/(.+)$/.exec(h)?.[1] ?? "");
  } catch {
    /* a malformed address */
  }
  const node = /^nodes\/([^/]+)\.(py|sql)$/.exec(path);
  if (node) return { mode: "graph", node: node[1] };
  const view = /^views\/([^/]+)\/(.+)$/.exec(path);
  if (view) return { mode: "views", view: { view: view[1], file: view[2], seq: 1 } };
  if (path.startsWith("lib/")) return { mode: "helper", helper: path };
  return { mode: "graph" };
}
const start = fromHash();
const hashFor = (m: Mode, helper: string | null) =>
  m === "views" ? "#views" : m === "history" ? "#history" : m === "helper" && helper ? `#files/${encodeURI(helper)}` : location.pathname + location.search;

/** A narrow window, where the side panel is a sheet over the main area. */
const narrow = () => window.matchMedia("(max-width: 1100px)").matches;

/** The side panel's tab, as it was left. */
function sideStartsOn(): SideTab {
  try {
    const kept = localStorage.getItem("exuvia-side");
    if (kept === "files" || kept === "history") return kept;
  } catch {
    /* private mode */
  }
  return "agent";
}

/** Whether the agent panel starts open: as it was left, else open where there is room beside the graph. */
function agentStartsOpen(): boolean {
  try {
    const kept = localStorage.getItem("exuvia-agent");
    if (kept) return kept === "open";
  } catch {
    /* private mode */
  }
  return window.matchMedia("(min-width: 1101px)").matches;
}

/**
 * A project's workbench. On a hub it first asks what this page may do there
 * (exuvia/sharing.py): someone the project isn't shared with gets a page to
 * request access, a viewer gets the workbench without what would change it.
 */
export default function App() {
  const [acc, setAcc] = useState<Access | null | undefined>(HUB_PROJECT ? undefined : null);
  useEffect(() => {
    // An older server has no /access: it shares nothing, so the page can do everything.
    if (HUB_PROJECT) access().then(setAcc).catch(() => setAcc(null));
  }, []);
  if (acc === undefined) return null;
  if (acc && acc.role === null) return <NoAccess access={acc} />;
  return (
    <AccessContext.Provider value={accessState(acc)}>
      {appView ? (
        <Suspense fallback={null}>
          <AppView view={appView} />
        </Suspense>
      ) : (
        <Workbench />
      )}
    </AccessContext.Provider>
  );
}

function Workbench() {
  const { canEdit, access: acc } = useAccess();
  const [sharing, setSharing] = useState(false);
  // Sharing is managed through the hub's API, which a share link alone doesn't open.
  const canShare = !!HUB_PROJECT && !!acc?.sharing && !(acc.link && !acc.user);
  const [project, setProject] = useState<Project | null>(null);
  const [cfg, setCfg] = useState<{ ai_enabled: boolean; model: string } | null>(null);
  const [selected, setSelected] = useState<string | null>(start.node ?? null);
  const [logs, setLogs] = useState<Record<string, string>>({});
  const [flash, setFlash] = useState<Record<string, number>>({});
  const [newOpen, setNewOpen] = useState(false);
  const [agentOpen, setAgentOpenState] = useState(agentStartsOpen);
  const setAgentOpen = useCallback((open: boolean) => {
    setAgentOpenState(open);
    try {
      localStorage.setItem("exuvia-agent", open ? "open" : "closed");
    } catch {
      /* private mode */
    }
  }, []);
  const [conn, setConn] = useState(true);
  const [mode, setModeState] = useState<Mode>(start.mode);
  const [helper, setHelper] = useState<string | null>(start.helper ?? null);
  const helperRef = useRef(helper);
  helperRef.current = helper;
  const setMode = useCallback((m: Mode, helperPath?: string) => {
    if (helperPath) setHelper((helperRef.current = helperPath));
    setModeState(m);
    history.replaceState(null, "", hashFor(m, helperRef.current));
  }, []);
  // What the side panel's Files and History tabs opened last.
  const [viewOpen, setViewOpen] = useState<OpenView | null>(start.view ?? null);
  // The view Views mode shows: what Share offers as an app first.
  const shownView = useRef<string | null>(null);
  const onShown = useCallback((v: string | null) => void (shownView.current = v), []);
  const [showCode, setShowCode] = useState(0);
  const [treePath, setTreePath] = useState<string | null>(null);
  const [rev, setRev] = useState<CodeRevision | null>(null);
  const [sideTabKept, setSideTabState] = useState<SideTab>(sideStartsOn);
  // A viewer has no agent (it changes the project): the panel opens on Files.
  const sideTab: SideTab = !canEdit && sideTabKept === "agent" ? "files" : sideTabKept;
  const setSideTab = useCallback((t: SideTab) => {
    setSideTabState(t);
    try {
      localStorage.setItem("exuvia-side", t);
    } catch {
      /* private mode */
    }
  }, []);
  const [dark, toggleTheme] = useColorMode();
  const agent = useRef<AgentHandle>(null);
  const [pkgs, setPkgs] = useState<PackagesState | null>(null);
  const [pkgOpen, setPkgOpen] = useState(false);
  // Nodes to re-run once the packages installed for them are ready.
  const [rerun, setRerun] = useState<string[]>([]);
  const refreshPkgs = useCallback(() => api.packages().then(setPkgs).catch(() => {}), []);

  const refresh = useCallback(() => api.project().then(setProject).catch(() => {}), []);
  const pending = useRef<number | null>(null);
  const refreshSoon = useCallback(() => {
    if (pending.current) return;
    pending.current = window.setTimeout(() => {
      pending.current = null;
      refresh();
    }, 120);
  }, [refresh]);

  useEffect(() => {
    refresh();
    api.config().then(setCfg);
    const es = new EventSource(apiUrl("/api/events"));
    es.onopen = () => setConn(true);
    es.onerror = () => setConn(false);
    es.onmessage = (m) => {
      const e = JSON.parse(m.data);
      if (e.type === "graph" || e.type === "run" || e.type === "hello") refreshSoon();
      if (e.type === "log") setLogs((l) => ({ ...l, [e.run_id]: (l[e.run_id] ?? "") + e.text }));
      if (e.type === "packages" || e.type === "hello") refreshPkgs();
    };
    const tick = setInterval(refreshSoon, 30_000); // keeps "next run in…" honest
    return () => {
      es.close();
      clearInterval(tick);
    };
  }, [refresh, refreshSoon, refreshPkgs]);

  useEffect(() => {
    if (!rerun.length || !pkgs) return;
    if (pkgs.status === "ready" || pkgs.status === "remote") {
      rerun.forEach((name) => api.run(name).catch(failed(`Couldn't run ${name}`)));
      setRerun([]);
    } else if (pkgs.status === "failed") {
      setRerun([]);
      setPkgOpen(true);
    }
  }, [pkgs, rerun]);

  /** The Install button on a node that failed importing a missing package. */
  const installFor = async (node: string, pkg: string) => {
    setPkgs(await api.packagesEdit({ add: [pkg] }));
    setRerun((r) => (r.includes(node) ? r : [...r, node]));
  };

  const nodes = useMemo(() => project?.nodes ?? [], [project]);
  useEffect(() => {
    if (project?.name) document.title = `${project.name} · exuvia`;
  }, [project?.name]);
  const sel = nodes.find((n) => n.name === selected) ?? null;
  // Changes whenever any node's or view's code does: the history lists are read again.
  const codeTick = useMemo(
    () => [...nodes.map((n) => n.code_hash), ...(project?.views ?? []).map((v) => v.hash)].join(),
    [nodes, project],
  );
  useEffect(() => {
    if (selected && project && !nodes.some((n) => n.name === selected)) setSelected(null);
  }, [nodes, project, selected]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    nodes.forEach((n) => (c[n.state] = (c[n.state] ?? 0) + 1));
    return c;
  }, [nodes]);
  const staleCount = (counts.stale ?? 0) + (counts.new ?? 0) + (counts.failed ?? 0);

  const touch = useCallback((name: string) => {
    setFlash((f) => ({ ...f, [name]: Date.now() }));
  }, []);

  /** A file picked in the side panel's Files tab, opened where it is edited. */
  const openFile = (ref: FileRef) => {
    setTreePath(ref.path);
    if (ref.kind === "node") {
      setMode("graph");
      setSelected(ref.node);
      setShowCode((n) => n + 1);
    } else if (ref.kind === "view") {
      setMode("views");
      setViewOpen((o) => ({ view: ref.view, file: ref.file, seq: (o?.seq ?? 1) + 1 }));
    } else setMode("helper", ref.path);
    // The panel is a sheet over the main area there: get it out of the way.
    if (narrow()) setAgentOpen(false);
  };
  /** A revision picked in the side panel's History tab: that file's versions in the main area. */
  const openRev = (r: CodeRevision) => {
    setRev(r);
    setMode("history");
    if (narrow()) setAgentOpen(false);
  };
  const openSide = (t: SideTab) => {
    setSideTab(t);
    setAgentOpen(true);
  };
  const toGraph = () => setMode("graph");
  const treeCurrent =
    mode === "graph" ? (sel?.path ?? null) : mode === "helper" ? helper : mode === "history" ? (rev?.path ?? null) : treePath?.startsWith("views/") ? treePath : null;

  const askAI = (prompt: string) => {
    setSideTab("agent");
    setAgentOpen(true);
    setTimeout(() => agent.current?.send(prompt), 0);
  };

  const aiOn = !!cfg?.ai_enabled;
  // The bars give up their words before their controls.
  const compact = useMediaQuery("(max-width: 900px)");
  const sheet = useMediaQuery("(max-width: 1100px)");
  const openNode = (name: string) => {
    setMode("graph");
    setSelected(name);
  };

  return (
    <div {...stylex.props(styles.app, !canEdit && styles.appBanner, embed && styles.appEmbed, embed && !canEdit && styles.appEmbedBanner)}>
      {/* The app bar: where you are, what you do with the project as a whole, and the app's own menus. */}
      {!embed && (
        <header {...stylex.props(styles.bar, styles.appBar)}>
          <Brand home={HUB_PROJECT ? "/" : undefined}>{project?.name ?? "…"}</Brand>
          <span {...stylex.props(styles.grow)} />
          <HStack gap={1} vAlign="center">
            {canShare && (
              <Button
                label="Share"
                icon={<IconShare />}
                isIconOnly={compact}
                tooltip="Who can open and change this project"
                onClick={() => setSharing(true)}
              />
            )}
            <DownloadMenu title="Download the project's code" groups={[codeGroup({ title: "Project" })]} />
          </HStack>
          <Divider orientation="vertical" xstyle={styles.rule} />
          <HStack gap={0.5} vAlign="center">
            <HelpMenu />
            <IconButton
              variant="ghost"
              label={dark ? "Light theme" : "Dark theme"}
              tooltip={dark ? "Light theme" : "Dark theme"}
              icon={dark ? <IconSun /> : <IconMoon />}
              onClick={toggleTheme}
            />
            <SignedInAs />
          </HStack>
        </header>
      )}
      {!canEdit && <ViewOnlyBanner />}

      <div {...stylex.props(styles.workspace, !agentOpen && styles.workspaceRail)}>
        <div {...stylex.props(styles.main)}>
          {/* The project's bar: its two faces, and what the graph is doing and can be told to do. */}
          <div {...stylex.props(styles.bar, styles.workBar)}>
            <TabList role="tablist" value={mode === "views" ? "views" : mode === "graph" ? "graph" : ""} onChange={(v) => setMode(v as Mode)} xstyle={styles.modes}>
              <Tab value="graph" label="Graph" />
              <Tab value="views" label="Views" endContent={project?.views.length ? <Badge label={project.views.length} /> : undefined} />
            </TabList>
            <span {...stylex.props(styles.grow)} />
            {!conn && <Badge variant="error" label="Reconnecting…" />}
            {/* The graph's own state and actions: with the graph, not with the views. */}
            {mode === "graph" && (<>
            <HStack gap={3} vAlign="center" xstyle={styles.counts}>
              {(["fresh", "stale", "new", "failed", "error", "running"] as const).map((s) =>
                counts[s] ? (
                  <HStack key={s} gap={1.5} vAlign="center">
                    <StateDot state={s} />
                    <Text type="supporting" color="secondary" hasTabularNumbers>
                      {counts[s]} {s}
                    </Text>
                  </HStack>
                ) : null,
              )}
            </HStack>
            {canEdit && (
              <HStack gap={1} vAlign="center">
                <Button
                  label="Packages"
                  icon={<IconPackage />}
                  isIconOnly={compact}
                  tooltip={pkgs?.requests.length ? "The agent is waiting for you to approve packages" : "The packages your nodes import"}
                  endContent={
                    pkgs?.requests.length ? (
                      <Badge variant="info" label={pkgs.requests.length} />
                    ) : pkgs?.status === "failed" ? (
                      <Badge variant="error" label="Failed" />
                    ) : pkgs?.status === "installing" || pkgs?.status === "pending" ? (
                      <Badge variant="info" label="Installing" />
                    ) : undefined
                  }
                  onClick={() => setPkgOpen(true)}
                />
                <Button label="New node" icon={<IconPlus />} isIconOnly={compact} tooltip="Add a node to the graph" onClick={() => setNewOpen(true)} />
                <Button
                  label="Run stale"
                  variant={staleCount ? "primary" : "secondary"}
                  icon={<IconPlay />}
                  isIconOnly={compact}
                  isDisabled={!staleCount}
                  tooltip={staleCount ? "Run every stale, new, or failed node" : "Every node is up to date"}
                  endContent={staleCount ? <Badge label={staleCount} /> : undefined}
                  onClick={() => api.runStale().catch(failed("Couldn't start the run"))}
                />
              </HStack>
            )}
            </>)}
          </div>

          {mode === "helper" && helper && project ? (
            <section {...stylex.props(styles.wide)}>
              <HelperEditor key={helper} path={helper} project={project} dark={dark} onClose={toGraph} onOpenNode={(name) => { openNode(name); setShowCode((n) => n + 1); }} />
            </section>
          ) : mode === "history" ? (
            <section {...stylex.props(styles.wide)}>
              <div {...stylex.props(styles.wideHead)}>
                <IconClock />
                <Heading level={4} accessibilityLevel={2}>Code history</Heading>
                <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.wideHint}>
                  Every saved version of the code. Pick one to compare it and restore it.
                </Text>
                <span {...stylex.props(styles.grow)} />
                <IconButton variant="ghost" size="sm" label="Close the history" tooltip="Close and go back to the graph" icon={<IconClose />} onClick={toGraph} />
              </div>
              <HistoryBrowser filter={{}} tick={codeTick} dark={dark} layout="side" path={rev?.path} rev={rev?.id} deletedFilter onRestored={() => refresh()} />
            </section>
          ) : mode === "views" ? (
            <Suspense fallback={<div {...stylex.props(styles.wide)} />}>
              <Views project={project} nodes={nodes} dark={dark} open={viewOpen} onOpenNode={openNode} onShown={onShown} />
            </Suspense>
          ) : (
            <div {...stylex.props(styles.graph, !agentOpen && styles.graphRail)}>
              <div {...stylex.props(styles.canvas)}>
                <ReactFlowProvider>
                  <Canvas nodes={nodes} selected={selected} flash={flash} onSelect={setSelected} />
                </ReactFlowProvider>
                {project && nodes.length === 0 && (
                  <div {...stylex.props(styles.emptyCanvas)}>
                    <EmptyState
                      xstyle={styles.emptyCard}
                      icon={<Mark size={40} />}
                      title="An empty graph"
                      headingLevel={2}
                      description={
                        !canEdit
                          ? "Nobody has added a node yet."
                          : aiOn
                            ? "Describe a pipeline to the agent, or add a node by hand."
                            : "Add your first node: a Python function or a SQL query."
                      }
                      actions={canEdit ? <Button variant="primary" label="New node" icon={<IconPlus />} onClick={() => setNewOpen(true)} /> : undefined}
                    />
                  </div>
                )}
              </div>
              <section {...stylex.props(styles.pane)}>
                {sel ? (
                  <Inspector
                    key={sel.name}
                    node={sel}
                    nodes={nodes}
                    logs={logs}
                    dark={dark}
                    aiEnabled={aiOn}
                    onAskAI={askAI}
                    onDeleted={() => setSelected(null)}
                    onInstall={(pkg) => installFor(sel.name, pkg)}
                    installing={rerun.includes(sel.name)}
                    showCode={showCode}
                  />
                ) : (
                  <Overview nodes={nodes} onSelect={setSelected} />
                )}
              </section>
            </div>
          )}
        </div>

        {agentOpen ? (
          <SidePanel
            ref={agent}
            sheet={sheet}
            enabled={aiOn}
            agent={canEdit}
            model={cfg?.model ?? ""}
            selected={sel}
            hasNodes={nodes.length > 0}
            onTouch={touch}
            onSelect={openNode}
            onCollapse={() => setAgentOpen(false)}
            tab={sideTab}
            onTab={setSideTab}
            files={<FileTree project={project} current={treeCurrent} onOpen={openFile} />}
            history={<HistoryRail tick={codeTick} current={mode === "history" ? (rev?.id ?? null) : null} onOpen={openRev} />}
          />
        ) : (
          <SideRail agent={canEdit} bar={sheet} onOpen={openSide} />
        )}
      </div>

      <Suspense fallback={null}>
        {sharing && HUB_PROJECT && (
          <ShareDialog
            project={HUB_PROJECT}
            name={project?.name}
            self={acc?.user?.uid ?? null}
            views={project?.views}
            view={mode === "views" ? (shownView.current ?? undefined) : undefined}
            onClose={() => setSharing(false)}
          />
        )}
        {pkgOpen && <PackagesDialog state={pkgs} onChange={setPkgs} onClose={() => setPkgOpen(false)} />}
        {newOpen && (
          <NewNodeDialog
            nodes={nodes}
            initialDeps={selected ? [selected] : []}
            onClose={() => setNewOpen(false)}
            onCreated={(name) => {
              setNewOpen(false);
              refresh().then(() => setSelected(name));
            }}
          />
        )}
      </Suspense>
      <Toasts />
    </div>
  );
}

/**
 * The workbench's frame. Two bars, from the outside in: the app bar (the
 * project and the app), then the project's bar (Graph and Views, and the
 * graph's own state and actions). Under them the main area, and the side
 * panel beside it.
 *
 *   > 1100px   main | side panel 340 (or its 40px rail)
 *              the graph: canvas | inspector
 *   <= 1100px  one column: canvas over inspector; the side panel is a bar
 *              along the bottom that opens as a sheet over them
 *   <= 900px   the bars' buttons lose their words (`compact`)
 */
const NARROW = "@media (max-width: 1100px)";
const COMPACT = "@media (max-width: 900px)";

const styles = stylex.create({
  app: { display: "grid", gridTemplateRows: "auto minmax(0, 1fr)", height: "100dvh" },
  appBanner: { gridTemplateRows: "auto auto minmax(0, 1fr)" },
  appEmbed: { gridTemplateRows: "minmax(0, 1fr)" },
  appEmbedBanner: { gridTemplateRows: "auto minmax(0, 1fr)" },
  bar: {
    display: "flex",
    alignItems: "center",
    gap: { default: spacingVars["--spacing-3"], [COMPACT]: spacingVars["--spacing-2"] },
    minWidth: 0,
    paddingInline: { default: spacingVars["--spacing-4"], [COMPACT]: spacingVars["--spacing-2"] },
    backgroundColor: colorVars["--color-background-surface"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  appBar: { paddingBlock: spacingVars["--spacing-2"] },
  // The tabs sit on the bar's bottom edge, so the bar itself has no padding there.
  workBar: { paddingBlock: 0, minHeight: "40px" },
  modes: { alignSelf: "flex-end" },
  grow: { flexGrow: 1 },
  rule: { alignSelf: "stretch", marginBlock: spacingVars["--spacing-1"] },
  // The state counts are on the canvas's nodes too: the first thing to go.
  counts: { display: { default: "flex", "@media (max-width: 700px)": "none" } },
  workspace: {
    display: "grid",
    gridTemplateColumns: { default: "minmax(0, 1fr) 340px", [NARROW]: "minmax(0, 1fr)" },
    gridTemplateRows: "minmax(0, 1fr)",
    minHeight: 0,
  },
  workspaceRail: {
    gridTemplateColumns: { default: "minmax(0, 1fr) 40px", [NARROW]: "minmax(0, 1fr)" },
    gridTemplateRows: { default: "minmax(0, 1fr)", [NARROW]: "minmax(0, 1fr) 40px" },
  },
  main: { display: "grid", gridTemplateRows: "auto minmax(0, 1fr)", minWidth: 0, minHeight: 0 },
  graph: {
    display: "grid",
    gridTemplateColumns: { default: "minmax(0, 1fr) clamp(380px, 32vw, 600px)", [NARROW]: "minmax(0, 1fr)" },
    gridTemplateRows: { default: "minmax(0, 1fr)", [NARROW]: "minmax(180px, 42%) minmax(0, 1fr)" },
    minHeight: 0,
  },
  graphRail: {
    gridTemplateColumns: { default: "minmax(0, 1fr) clamp(400px, 38vw, 680px)", [NARROW]: "minmax(0, 1fr)" },
  },
  canvas: { position: "relative", minWidth: 0, minHeight: 0 },
  emptyCanvas: { position: "absolute", inset: 0, display: "grid", placeItems: "center", pointerEvents: "none" },
  emptyCard: { pointerEvents: "auto" },
  pane: {
    display: "flex",
    flexDirection: "column",
    minWidth: 0,
    minHeight: 0,
    backgroundColor: colorVars["--color-background-surface"],
    borderColor: colorVars["--color-border"],
    borderStyle: "solid",
    borderWidth: 0,
    borderInlineStartWidth: { default: "1px", [NARROW]: 0 },
    borderTopWidth: { default: 0, [NARROW]: "1px" },
  },
  wide: { display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, backgroundColor: colorVars["--color-background-surface"] },
  wideHead: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    paddingBlock: spacingVars["--spacing-2"],
    paddingInline: spacingVars["--spacing-4"],
    color: colorVars["--color-text-secondary"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  wideHint: { display: { default: "block", [NARROW]: "none" } },
});
