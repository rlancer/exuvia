import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { api, apiUrl, HUB_PROJECT, type PackagesState, type Project } from "./api";
import { Canvas } from "./Canvas";
import { Inspector, Overview } from "./Inspector";
import { AgentPanel, type AgentHandle } from "./AgentPanel";
import { NewNodeDialog } from "./NewNodeDialog";
import { PackagesDialog } from "./PackagesDialog";
import { HelpersDialog } from "./HelpersDialog";
import { HistoryDialog } from "./History";
import { Views, showViewNext } from "./Views";
import { Files } from "./Files";
import { SignedInAs } from "./Me";
import { HelpMenu } from "./Help";
import { useTheme } from "./theme";
import { Brand, Mark } from "./Brand";
import { IconClock, IconCode, IconMoon, IconPackage, IconPlay, IconPlus, IconSpark, IconSun } from "./icons";
import { failed, Toasts } from "./toast";

const embed = new URLSearchParams(location.search).has("embed");

type Mode = "graph" | "views" | "files";
const modeFromHash = (): Mode => (location.hash === "#views" ? "views" : location.hash.startsWith("#files") ? "files" : "graph");
/** The file open in Files mode, kept in the address: `#files/nodes/prices.py`. */
const fileFromHash = (): string | null => {
  const m = /^#files\/(.+)$/.exec(location.hash);
  try {
    return m ? decodeURI(m[1]) : null;
  } catch {
    return null; // a malformed address
  }
};
const hashFor = (m: Mode, file: string | null) =>
  m === "views" ? "#views" : m === "files" ? (file ? `#files/${encodeURI(file)}` : "#files") : location.pathname + location.search;

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
  const [selected, setSelected] = useState<string | null>(null);
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
  const [mode, setModeState] = useState<Mode>(modeFromHash);
  const [file, setFileState] = useState<string | null>(fileFromHash);
  const fileRef = useRef(file);
  fileRef.current = file;
  const setMode = useCallback((m: Mode) => {
    setModeState(m);
    history.replaceState(null, "", hashFor(m, fileRef.current));
  }, []);
  const setFile = useCallback((f: string | null) => {
    setFileState(f);
    history.replaceState(null, "", hashFor("files", f));
  }, []);
  const [dark, toggleTheme] = useTheme();
  const agent = useRef<AgentHandle>(null);
  const [pkgs, setPkgs] = useState<PackagesState | null>(null);
  const [pkgOpen, setPkgOpen] = useState(false);
  const [helpersOpen, setHelpersOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
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
  // Changes whenever any node's or view's code does: the history dialog reads its list again.
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

  const askAI = (prompt: string) => {
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
          <button
            className={mode === "files" ? "on" : ""}
            onClick={() => setMode("files")}
            role="tab"
            aria-selected={mode === "files"}
            title="Browse and edit the files of the graph and the views"
          >
            Files
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
        <button className="btn has-icon" onClick={() => setHelpersOpen(true)} title="Browse the project's shared Python helpers" aria-label="Helpers">
          <IconCode /> <span className="lbl">Helpers</span>
        </button>
        <button
          className="btn has-icon"
          onClick={() => setHistoryOpen(true)}
          title="Every saved version of the project's code, and deleted nodes and views to bring back"
          aria-label="History"
        >
          <IconClock /> <span className="lbl">History</span>
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
        {mode === "files" ? (
          <Files
            project={project}
            dark={dark}
            file={file}
            onFile={setFile}
            onOpenNode={(name) => {
              setMode("graph");
              setSelected(name);
            }}
            onOpenView={(name) => {
              showViewNext(name);
              setMode("views");
            }}
          />
        ) : mode === "views" ? (
          <Views
            project={project}
            nodes={nodes}
            dark={dark}
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
            onSelect={setSelected}
            onCollapse={() => setAgentOpen(false)}
          />
        ) : (
          <button className="agent-collapsed-rail" onClick={() => setAgentOpen(true)} title="Open the agent">
            <IconSpark /> Agent
          </button>
        )}
      </div>

      {pkgOpen && <PackagesDialog state={pkgs} onChange={setPkgs} onClose={() => setPkgOpen(false)} />}
      {helpersOpen && <HelpersDialog onClose={() => setHelpersOpen(false)} />}
      {historyOpen && (
        <HistoryDialog
          dark={dark}
          tick={codeTick}
          onClose={() => setHistoryOpen(false)}
          onRestored={(rev) => {
            // A node brought back: show it once the graph has it.
            refresh().then(() => rev.node && mode === "graph" && setSelected(rev.node));
          }}
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
      <Toasts />
    </div>
  );
}
