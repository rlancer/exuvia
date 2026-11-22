import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { api, apiUrl, HUB_PROJECT, type CodeRevision, type PackagesState, type Project } from "./api";
import { Canvas } from "./Canvas";
import { Inspector, Overview } from "./Inspector";
import { AgentPanel, type AgentHandle, type SideTab } from "./AgentPanel";
import { NewNodeDialog } from "./NewNodeDialog";
import { PackagesDialog } from "./PackagesDialog";
import { HistoryBrowser, HistoryRail } from "./History";
import { Views, type OpenView } from "./Views";
import { FileTree, HelperEditor } from "./Files";
import type { FileRef } from "./tree";
import { SignedInAs } from "./Me";
import { HelpMenu } from "./Help";
import { useTheme } from "./theme";
import { Brand, Mark } from "./Brand";
import { IconClock, IconClose, IconFolder, IconMoon, IconPackage, IconPlay, IconPlus, IconSpark, IconSun } from "./icons";
import { failed, Toasts } from "./toast";

const embed = new URLSearchParams(location.search).has("embed");

/** What the main area shows: the graph, the views, a shared helper, or the code's history. */
type Mode = "graph" | "views" | "helper" | "history";

/**
 * Where the address points on load. `#files/<path>` opens a file where it
 * lives: a node on the graph, a view file in its Code tab, a helper in the
 * editor; `#history` the code's history.
 */
function fromHash(): { mode: Mode; node?: string; view?: OpenView; helper?: string } {
  const h = location.hash;
  if (h === "#views") return { mode: "views" };
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

export default function App() {
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
  const [showCode, setShowCode] = useState(0);
  const [treePath, setTreePath] = useState<string | null>(null);
  const [rev, setRev] = useState<CodeRevision | null>(null);
  const [sideTab, setSideTabState] = useState<SideTab>(sideStartsOn);
  const setSideTab = useCallback((t: SideTab) => {
    setSideTabState(t);
    try {
      localStorage.setItem("exuvia-side", t);
    } catch {
      /* private mode */
    }
  }, []);
  const [dark, toggleTheme] = useTheme();
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
  const mainCls = `main ${agentOpen ? "" : "agent-collapsed"}`;

  return (
    <div className={`app ${embed ? "embed" : ""}`}>
      <header className="topbar">
        <Brand home={HUB_PROJECT ? "/" : undefined}>{project?.name ?? "…"}</Brand>
        <div className="seg mode-seg" role="tablist" aria-label="Mode">
          <button className={mode === "graph" ? "on" : ""} onClick={() => setMode("graph")} role="tab" aria-selected={mode === "graph"}>
            Graph
          </button>
          <button className={mode === "views" ? "on" : ""} onClick={() => setMode("views")} role="tab" aria-selected={mode === "views"}>
            Views{project?.views.length ? <span className="n">{project.views.length}</span> : null}
          </button>
        </div>
        <div className="counts">
          {(["fresh", "stale", "new", "failed", "error", "running"] as const).map((s) =>
            counts[s] ? (
              <span key={s} className={`s-${s}`}>
                <span className="dot" /> {counts[s]} <span className="word">{s}</span>
              </span>
            ) : null,
          )}
          {!conn && <span className="offline" role="status">reconnecting…</span>}
        </div>
        <span className="spacer" />
        <button
          className="btn has-icon"
          onClick={() => api.runStale().catch(failed("Couldn't start the run"))}
          disabled={!staleCount}
          title={staleCount ? "Run every stale, new, or failed node" : "Every node is up to date"}
          aria-label="Run stale"
        >
          <IconPlay size={11} /> <span className="lbl">Run stale</span>
          {staleCount ? <span>({staleCount})</span> : null}
        </button>
        <button className="btn has-icon" onClick={() => setNewOpen(true)} title="Add a node to the graph" aria-label="New node">
          <IconPlus size={12} /> <span className="lbl">Node</span>
        </button>
        <button
          className={`btn has-icon pk-btn pk-${pkgs?.status ?? "none"}`}
          onClick={() => setPkgOpen(true)}
          title={pkgs?.requests.length ? "The agent is waiting for you to approve packages" : "The packages your nodes import"}
          aria-label="Packages"
        >
          <IconPackage /> <span className="lbl">Packages</span>
          {pkgs?.requests.length ? <span className="pk-badge">{pkgs.requests.length}</span> : null}
          {pkgs?.status === "installing" || pkgs?.status === "pending" ? <span className="pk-spin" /> : null}
          {pkgs?.status === "failed" ? <span className="pk-bang">!</span> : null}
        </button>
        <HelpMenu />
        <button className="btn ghost icon-btn" onClick={toggleTheme} title={dark ? "Light theme" : "Dark theme"} aria-label={dark ? "Light theme" : "Dark theme"}>
          {dark ? <IconSun /> : <IconMoon />}
        </button>
        <SignedInAs />
      </header>

      <div className={mainCls}>
        {mode === "helper" && helper && project ? (
          <section className="main-wide">
            <HelperEditor key={helper} path={helper} project={project} dark={dark} onClose={toGraph} onOpenNode={(name) => { setMode("graph"); setSelected(name); setShowCode((n) => n + 1); }} />
          </section>
        ) : mode === "history" ? (
          <section className="main-wide main-history">
            <div className="main-wide-head">
              <IconClock />
              <h3>Code history</h3>
              <span className="hint">Every saved version of the code. Pick one to compare it and restore it.</span>
              <span className="spacer" />
              <button className="btn ghost icon-btn sm" onClick={toGraph} title="Close and go back to the graph" aria-label="Close the history">
                <IconClose />
              </button>
            </div>
            <HistoryBrowser
              filter={{}}
              tick={codeTick}
              dark={dark}
              layout="side"
              path={rev?.path}
              rev={rev?.id}
              deletedFilter
              onRestored={() => refresh()}
            />
          </section>
        ) : mode === "views" ? (
          <Views
            project={project}
            nodes={nodes}
            dark={dark}
            open={viewOpen}
            onOpenNode={(name) => {
              setMode("graph");
              setSelected(name);
            }}
          />
        ) : (
          <>
            <div className="canvas-wrap">
              <ReactFlowProvider>
                <Canvas nodes={nodes} selected={selected} flash={flash} onSelect={setSelected} />
              </ReactFlowProvider>
              {project && nodes.length === 0 && (
                <div className="empty-canvas">
                  <div className="empty-card">
                    <Mark size={40} />
                    <h2>An empty graph</h2>
                    <p>
                      {aiOn ? "Describe a pipeline to the agent, or add a node by hand." : "Add your first node — a Python function or a SQL query."}
                    </p>
                    <button className="btn primary" onClick={() => setNewOpen(true)}><IconPlus size={12} /> New node</button>
                  </div>
                </div>
              )}
            </div>

            <section className="pane">
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
          </>
        )}

        {agentOpen ? (
          <AgentPanel
            ref={agent}
            enabled={aiOn}
            model={cfg?.model ?? ""}
            selected={sel}
            hasNodes={nodes.length > 0}
            onTouch={touch}
            onSelect={(name) => {
              setMode("graph");
              setSelected(name);
            }}
            onCollapse={() => setAgentOpen(false)}
            tab={sideTab}
            onTab={setSideTab}
            files={<FileTree project={project} current={treeCurrent} onOpen={openFile} />}
            history={<HistoryRail tick={codeTick} current={mode === "history" ? (rev?.id ?? null) : null} onOpen={openRev} />}
          />
        ) : (
          <div className="agent-collapsed-rail" role="group" aria-label="Side panel">
            <button onClick={() => openSide("agent")} title="Open the agent">
              <IconSpark /> Agent
            </button>
            <button onClick={() => openSide("files")} title="Open the files of the graph and the views">
              <IconFolder /> Files
            </button>
            <button onClick={() => openSide("history")} title="Open the code's history">
              <IconClock /> History
            </button>
          </div>
        )}
      </div>

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
      <Toasts />
    </div>
  );
}
