import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
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
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import { access, api, apiUrl, HUB_PROJECT, type Access, type CodeRevision, type PackagesState, type Project } from "./api";
import { AccessContext, accessState, NoAccess, useAccess, ViewOnlyBanner } from "./Access";
import { Canvas } from "./Canvas";
import { Inspector, Overview } from "./Inspector";
import { SidePanel, SideRail, type AgentHandle, type SideTab } from "./AgentPanel";
import { HistoryBrowser, HistoryRail } from "./History";
import { TagBrowser } from "./Tags";
import type { OpenView } from "./Views";
import { FileTree, HelperEditor } from "./Files";
import { Schedule } from "./Schedule";
import { codeGroup, DownloadMenu } from "./Export";
import type { FileRef } from "./tree";
import { SignedInAs } from "./Me";
import { HelpMenu } from "./Help";
import { HubNav } from "./HubNav";
import { useColorMode } from "./theme/colorMode";
import { Brand, Mark } from "./Brand";
import { IconClock, IconClose, IconGraph, IconKey, IconNotebook, IconTag, IconMoon, IconPackage, IconPlay, IconPlus, IconShare, IconSun } from "./icons";
import { StateDot } from "./ui/state";
import { failed, Toasts } from "./toast";
import { appViewOf } from "./appMode";
import { barLayout } from "./barLayout";
import { format, landing, parse, routeOf, type Layout, type Mode, type Route } from "./route";
import { oneOf, usePersisted } from "./ui/usePersisted";

/** `#env` opens the Environment dialog; `#env=<NAME>` on that variable (the
 * link request_secret gives, so a person sets a secret the agent asked for). */
function envOf(hash: string): { name?: string } | null {
  const r = parse(hash);
  return r.page === "env" ? (r.name ? { name: r.name } : {}) : null;
}

// What is not on the first screen is fetched when it is opened: the Views
// mode and the dialogs.
const Views = lazy(() => import("./Views").then((m) => ({ default: m.Views })));
const ShareDialog = lazy(() => import("./Share").then((m) => ({ default: m.ShareDialog })));
const NewNodeDialog = lazy(() => import("./NewNodeDialog").then((m) => ({ default: m.NewNodeDialog })));
const PackagesDialog = lazy(() => import("./PackagesDialog").then((m) => ({ default: m.PackagesDialog })));
const EnvDialog = lazy(() => import("./EnvDialog").then((m) => ({ default: m.EnvDialog })));
const AppView = lazy(() => import("./AppView"));
const Notebook = lazy(() => import("./Notebook").then((m) => ({ default: m.Notebook })));

const embed = new URLSearchParams(location.search).has("embed");
/** `?app=<view>`: that one view as an app, without the workbench (AppView.tsx). */
const appView = appViewOf(location.search);

/** Where the address points on load (route.ts). */
const start = landing(parse(location.hash));
/** Leave the address at a route; the graph's is the page's own, with no `#`. */
const goTo = (r: Route) => history.replaceState(null, "", format(r) || location.pathname + location.search);

/** The states the project's bar counts, in its order. */
const COUNTED = ["fresh", "stale", "new", "failed", "error", "running"] as const;

/** An element's width, as it is laid out now and whenever it changes. */
function useWidth<T extends HTMLElement>(): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null);
  const [width, setWidth] = useState(() => window.innerWidth);
  useLayoutEffect(() => {
    if (!el) return;
    setWidth(el.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
  return [setEl, width];
}

/** A narrow window, where the side panel is a sheet over the main area. */
const narrow = () => window.matchMedia("(max-width: 1100px)").matches;

const sideTabs = oneOf<SideTab>("agent", "files", "history");
const openClosed = oneOf("open", "closed");
const layouts = oneOf<Layout>("canvas", "notebook");

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
  // The agent panel: as it was left, else open where there is room beside the graph.
  const [agentKept, setAgentKept] = usePersisted("exuvia-agent", () => (window.matchMedia("(min-width: 1101px)").matches ? "open" : "closed"), openClosed);
  const agentOpen = agentKept === "open";
  const setAgentOpen = useCallback((open: boolean) => setAgentKept(open ? "open" : "closed"), [setAgentKept]);
  const [conn, setConn] = useState(true);
  const [mode, setModeState] = useState<Mode>(start.mode);
  // The graph's layout: as the address says, else as this browser left it for the project, else the canvas.
  const [layoutPicked, setLayoutPicked] = useState<Layout | null>(start.layout ?? null);
  const [helper, setHelper] = useState<string | null>(start.helper ?? null);
  const helperRef = useRef(helper);
  helperRef.current = helper;
  const setMode = useCallback((m: Mode, helperPath?: string) => {
    if (helperPath) setHelper((helperRef.current = helperPath));
    setModeState(m);
    goTo(routeOf(m, helperRef.current));
  }, []);
  // What the side panel's Files and History tabs opened last.
  const [viewOpen, setViewOpen] = useState<OpenView | null>(start.view ? { ...start.view, seq: 1 } : null);
  // The view Views mode shows: what Share offers as an app first.
  const shownView = useRef<string | null>(null);
  const onShown = useCallback((v: string | null) => void (shownView.current = v), []);
  const [showCode, setShowCode] = useState(0);
  const [treePath, setTreePath] = useState<string | null>(null);
  const [rev, setRev] = useState<CodeRevision | null>(null);
  // The tag open in the main area's history, in place of a revision.
  const [tag, setTag] = useState<string | null>(null);
  // Counts tags made or deleted (here or by anyone else): the Tags list is read again.
  const [tagTick, setTagTick] = useState(0);
  const [sideTabKept, setSideTab] = usePersisted<SideTab>("exuvia-side", "agent", sideTabs);
  // A viewer has no agent (it changes the project): the panel opens on Files.
  const sideTab: SideTab = !canEdit && sideTabKept === "agent" ? "files" : sideTabKept;
  const [dark, toggleTheme] = useColorMode();
  const agent = useRef<AgentHandle>(null);
  const [pkgs, setPkgs] = useState<PackagesState | null>(null);
  const [pkgOpen, setPkgOpen] = useState(false);
  // The Environment dialog: closed (null), or open, on a variable when a link named one.
  const [envOpen, setEnvOpen] = useState<{ name?: string } | null>(() => envOf(location.hash));
  // Requests to set a secret (the agent's request_secret), for the button's count.
  const [envRequests, setEnvRequests] = useState(0);
  const refreshEnv = useCallback(() => api.env().then((s) => setEnvRequests(s.requests.length)).catch(() => {}), []);
  useEffect(() => {
    if (envOf(location.hash)) goTo({ page: "graph" });
    const onHash = () => {
      const e = envOf(location.hash);
      if (!e) return;
      setEnvOpen(e);
      goTo({ page: "graph" });
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
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
      if (e.type === "env" || e.type === "hello") refreshEnv();
      if (e.type === "tags") setTagTick((n) => n + 1);
    };
    const tick = setInterval(refreshSoon, 30_000); // keeps "next run in…" honest
    return () => {
      es.close();
      clearInterval(tick);
    };
  }, [refresh, refreshSoon, refreshPkgs, refreshEnv]);

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
  const projectName = project?.name ?? null;
  const [layoutKept, setLayoutKept] = usePersisted<Layout | null>(projectName ? `exuvia-graph-layout:${projectName}` : null, null, layouts);
  const layout: Layout = layoutPicked ?? layoutKept ?? "canvas";
  const setLayout = useCallback(
    (l: Layout) => {
      setLayoutPicked(l);
      setLayoutKept(l);
    },
    [setLayoutKept],
  );
  // The notebook has an address, with the cell picked; the canvas keeps the plain one.
  useEffect(() => {
    if (mode !== "graph") return;
    if (layout === "notebook") goTo({ page: "notebook", node: selected });
    else if (parse(location.hash).page === "notebook") goTo({ page: "graph" });
  }, [mode, layout, selected]);
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
  const scheduled = nodes.filter((n) => n.schedule).length;

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
    setTag(null);
    setMode("history");
    if (narrow()) setAgentOpen(false);
  };
  /** A tag picked in the History tab's Tags list: its files beside the code now, in the main area. */
  const openTag = (name: string) => {
    setTag(name);
    setMode("history");
    if (narrow()) setAgentOpen(false);
  };
  const openSide = (t: SideTab) => {
    setSideTab(t);
    setAgentOpen(true);
  };
  const toGraph = () => setMode("graph");
  const treeCurrent =
    mode === "graph" ? (sel?.path ?? null) : mode === "helper" ? helper : mode === "history" ? (tag ? null : (rev?.path ?? null)) : treePath?.startsWith("views/") ? treePath : null;

  const askAI = (prompt: string) => {
    setSideTab("agent");
    setAgentOpen(true);
    setTimeout(() => agent.current?.send(prompt), 0);
  };

  const aiOn = !!cfg?.ai_enabled;
  // The bars give up their words before their controls.
  const compact = useMediaQuery("(max-width: 900px)");
  // A phone: the bar keeps its tabs' words before anything that can be told another way.
  const phone = useMediaQuery("(max-width: 560px)");
  // The project's bar shows the richest layout that fits its width (barLayout.ts).
  const [barRef, barPx] = useWidth<HTMLDivElement>();
  const countedStates = COUNTED.filter((s) => counts[s]).length;
  const fit = barLayout(barPx, { states: mode === "graph" ? countedStates : 0, buttons: canEdit && mode === "graph", toggle: mode === "graph" && nodes.length > 0 && !phone });
  const sheet = useMediaQuery("(max-width: 1100px)");
  const openNode = (name: string) => {
    setMode("graph");
    setSelected(name);
  };
  /** A node's code, to change it: the node in the inspector, on its Code tab. */
  const editNode = useCallback((name: string) => {
    setSelected(name);
    setShowCode((n) => n + 1);
  }, []);
  /** A view picked on the canvas: rendered in Views mode. */
  const openView = useCallback(
    (name: string) => {
      setMode("views");
      setViewOpen((o) => ({ view: name, seq: (o?.seq ?? 1) + 1 }));
    },
    [setMode],
  );

  return (
    <div {...stylex.props(styles.app, embed && styles.appEmbed)}>
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
      <div {...stylex.props(styles.body)}>
        {/* On a hub, its left nav: home, admin and the other projects (HubNav.tsx). */}
        {HUB_PROJECT && !embed && <HubNav place={{ page: "project", id: HUB_PROJECT }} />}
        <div {...stylex.props(styles.content, !canEdit && styles.contentBanner)}>
          {!canEdit && <ViewOnlyBanner />}

          <div {...stylex.props(styles.workspace, !agentOpen && styles.workspaceRail)}>
            <div {...stylex.props(styles.main)}>
              {/* The project's bar: its two faces, and what the graph is doing and can be told to do. */}
              <div ref={barRef} {...stylex.props(styles.bar, styles.workBar)}>
                <TabList role="tablist" value={mode === "views" || mode === "graph" || mode === "schedule" ? mode : ""} onChange={(v) => setMode(v as Mode)} xstyle={[styles.modes, phone && styles.modesScroll]}>
                  <Tab value="graph" label="Graph" />
                  <Tab value="views" label="Views" endContent={project?.views.length && !phone ? <Badge label={project.views.length} /> : undefined} />
                  <Tab value="schedule" label="Schedule" endContent={scheduled && !phone ? <Badge label={scheduled} /> : undefined} />
                </TabList>
                {/* Two ways to read the same graph: drawn, or top to bottom as a notebook. On a phone, one button that switches. */}
                {mode === "graph" && nodes.length > 0 &&
                  (phone ? (
                    <IconButton
                      variant="ghost"
                      label={layout === "notebook" ? "Show as a canvas" : "Show as a notebook"}
                      tooltip={layout === "notebook" ? "Show the graph as a canvas" : "Show the graph as a notebook"}
                      icon={layout === "notebook" ? <IconGraph /> : <IconNotebook />}
                      onClick={() => setLayout(layout === "notebook" ? "canvas" : "notebook")}
                    />
                  ) : (
                    <SegmentedControl size="sm" value={layout} onChange={(v) => setLayout(v as Layout)} label="Show the graph as">
                      <SegmentedControlItem value="canvas" label="Canvas" icon={<IconGraph />} isLabelHidden={!fit.toggleLabels} />
                      <SegmentedControlItem value="notebook" label="Notebook" icon={<IconNotebook />} isLabelHidden={!fit.toggleLabels} />
                    </SegmentedControl>
                  ))}
                <span {...stylex.props(styles.grow)} />
                {!conn && <Badge variant="error" label="Reconnecting…" />}
                {/* The graph's own state and actions: with the graph, not with the views. */}
                {mode === "graph" && (<>
                {fit.counts && !phone && (
                <HStack gap={3} vAlign="center" xstyle={styles.counts}>
                  {COUNTED.map((s) =>
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
                )}
                {canEdit && (
                  <HStack gap={1} vAlign="center">
                    <Button
                      label="Packages"
                      icon={<IconPackage />}
                      isIconOnly={!fit.buttonLabels}
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
                    <Button
                      label="Environment"
                      icon={<IconKey />}
                      isIconOnly={!fit.buttonLabels}
                      tooltip={envRequests ? "The agent is waiting for you to set a secret" : "API keys, tokens and settings your nodes read from os.environ"}
                      endContent={envRequests ? <Badge variant="info" label={envRequests} /> : undefined}
                      onClick={() => setEnvOpen({})}
                    />
                    <Button label="New node" icon={<IconPlus />} isIconOnly={!fit.buttonLabels} tooltip="Add a node to the graph" onClick={() => setNewOpen(true)} />
                    <Button
                      label="Run stale"
                      variant={staleCount ? "primary" : "secondary"}
                      icon={<IconPlay />}
                      isIconOnly={!fit.buttonLabels}
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
                    {tag ? <IconTag /> : <IconClock />}
                    <Heading level={4} accessibilityLevel={2}>{tag ? `Tag ${tag}` : "Code history"}</Heading>
                    <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.wideHint}>
                      {tag ? "The code as tagged, beside the code now." : "Every saved version of the code. Pick one to compare it and restore it."}
                    </Text>
                    <span {...stylex.props(styles.grow)} />
                    {tag && <Button size="sm" variant="ghost" label="All history" tooltip="Every saved version of the code" onClick={() => setTag(null)} />}
                    <IconButton variant="ghost" size="sm" label="Close the history" tooltip="Close and go back to the graph" icon={<IconClose />} onClick={toGraph} />
                  </div>
                  {tag ? (
                    <TagBrowser
                      name={tag}
                      tick={codeTick}
                      dark={dark}
                      onRestored={() => refresh()}
                      onDeleted={() => {
                        setTag(null);
                        setTagTick((n) => n + 1);
                      }}
                    />
                  ) : (
                    <HistoryBrowser filter={{}} tick={codeTick} dark={dark} layout="side" path={rev?.path} rev={rev?.id} deletedFilter onRestored={() => refresh()} />
                  )}
                </section>
              ) : mode === "schedule" ? (
                <Schedule tick={project} onOpenNode={openNode} />
              ) : mode === "views" ? (
                <Suspense fallback={<div {...stylex.props(styles.wide)} />}>
                  <Views project={project} nodes={nodes} dark={dark} open={viewOpen} onOpenNode={openNode} onShown={onShown} />
                </Suspense>
              ) : (
                <div {...stylex.props(styles.graph, !agentOpen && styles.graphRail, layout === "notebook" && styles.graphNotebook)}>
                  <div {...stylex.props(styles.canvas)}>
                    {layout === "notebook" ? (
                      <Suspense fallback={null}>
                        {project && (
                          <Notebook
                            project={project}
                            selected={selected}
                            flash={flash}
                            dark={dark}
                            onSelect={setSelected}
                            onOpenView={openView}
                            onEdit={editNode}
                          />
                        )}
                      </Suspense>
                    ) : (
                      <ReactFlowProvider>
                        {project && <Canvas project={project} selected={selected} flash={flash} dark={dark} onSelect={setSelected} onOpenView={openView} />}
                      </ReactFlowProvider>
                    )}
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
                history={
                  <HistoryRail
                    tick={codeTick}
                    tagTick={tagTick}
                    current={mode === "history" && !tag ? (rev?.id ?? null) : null}
                    onOpen={openRev}
                    currentTag={mode === "history" ? tag : null}
                    onOpenTag={openTag}
                  />
                }
              />
            ) : (
              <SideRail agent={canEdit} bar={sheet} onOpen={openSide} />
            )}
          </div>
        </div>
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
        {envOpen && (
          <EnvDialog
            key={envOpen.name ?? ""}
            initialName={envOpen.name}
            onChange={(s) => setEnvRequests(s.requests.length)}
            onClose={() => setEnvOpen(null)}
          />
        )}
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
 *   <= 900px   the app bar's buttons lose their words (`compact`)
 *   the project's bar, by its own width: Canvas | Notebook, then its
 *              buttons, lose their words, then the state counts go (`fit`)
 *   <= 560px   the tabs lose their counts and Canvas | Notebook is one
 *              button (`phone`); on the smallest phones the tabs scroll
 */
const NARROW = "@media (max-width: 1100px)";
const COMPACT = "@media (max-width: 900px)";
const PHONE = "@media (max-width: 560px)";

const styles = stylex.create({
  app: { display: "grid", gridTemplateRows: "auto minmax(0, 1fr)", height: "100dvh" },
  appEmbed: { gridTemplateRows: "minmax(0, 1fr)" },
  // Under the app bar: the hub's left nav (on a hub), beside the project.
  body: { display: "flex", minHeight: 0 },
  content: { display: "grid", gridTemplateRows: "minmax(0, 1fr)", flexGrow: 1, minWidth: 0, minHeight: 0 },
  contentBanner: { gridTemplateRows: "auto minmax(0, 1fr)" },
  bar: {
    display: "flex",
    alignItems: "center",
    gap: { default: spacingVars["--spacing-3"], [COMPACT]: spacingVars["--spacing-2"], [PHONE]: spacingVars["--spacing-1"] },
    minWidth: 0,
    paddingInline: { default: spacingVars["--spacing-4"], [COMPACT]: spacingVars["--spacing-2"], [PHONE]: spacingVars["--spacing-1"] },
    backgroundColor: colorVars["--color-background-surface"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  appBar: { paddingBlock: spacingVars["--spacing-2"] },
  // The tabs sit on the bar's bottom edge, so the bar itself has no padding there.
  workBar: { paddingBlock: 0, minHeight: "40px" },
  // The tabs keep their words: what is beside them gives way first (barLayout.ts).
  modes: { alignSelf: "flex-end", flexShrink: 0 },
  // On the smallest phones they scroll rather than be cut off.
  modesScroll: { flexShrink: 1, minWidth: 0, overflowX: "auto", scrollbarWidth: "none" },
  grow: { flexGrow: 1 },
  rule: { alignSelf: "stretch", marginBlock: spacingVars["--spacing-1"] },
  // The state counts are on the canvas's nodes too: the last of the bar's layouts leaves them out.
  counts: { flexShrink: 0, whiteSpace: "nowrap" },
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
  // The notebook is read more than the inspector at a narrow width: it gets the larger share.
  graphNotebook: {
    gridTemplateRows: { default: "minmax(0, 1fr)", [NARROW]: "minmax(0, 3fr) minmax(0, 2fr)" },
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
