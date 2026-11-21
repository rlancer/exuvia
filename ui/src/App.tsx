import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { api, apiUrl, HUB_PROJECT, type Project } from "./api";
import { Canvas } from "./Canvas";
import { Inspector, Overview } from "./Inspector";
import { AgentPanel, type AgentHandle } from "./AgentPanel";
import { NewNodeDialog } from "./NewNodeDialog";
import { Views } from "./Views";
import { SignedInAs } from "./Me";
import { useTheme } from "./theme";

const embed = new URLSearchParams(location.search).has("embed");

type Mode = "graph" | "views";
const modeFromHash = (): Mode => (location.hash === "#views" ? "views" : "graph");

export default function App() {
  const [project, setProject] = useState<Project | null>(null);
  const [cfg, setCfg] = useState<{ ai_enabled: boolean; model: string } | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [logs, setLogs] = useState<Record<string, string>>({});
  const [flash, setFlash] = useState<Record<string, number>>({});
  const [newOpen, setNewOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(true);
  const [conn, setConn] = useState(true);
  const [mode, setModeState] = useState<Mode>(modeFromHash);
  const setMode = useCallback((m: Mode) => {
    setModeState(m);
    history.replaceState(null, "", m === "views" ? "#views" : location.pathname + location.search);
  }, []);
  const [dark, toggleTheme] = useTheme();
  const agent = useRef<AgentHandle>(null);

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
    };
    const tick = setInterval(refreshSoon, 30_000); // keeps "next run in…" honest
    return () => {
      es.close();
      clearInterval(tick);
    };
  }, [refresh, refreshSoon]);

  const nodes = useMemo(() => project?.nodes ?? [], [project]);
  const sel = nodes.find((n) => n.name === selected) ?? null;
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
        <div className="brand">
          <svg width="20" height="20" viewBox="0 0 32 32" aria-hidden>
            <path d="M5 9c9 0 11 14 22 14M5 23c9 0 11-14 22-14" stroke="var(--accent)" strokeWidth="3.2" fill="none" strokeLinecap="round" />
          </svg>
          {HUB_PROJECT ? <a className="home-link" href="/" title="All projects">exuvia</a> : "exuvia"}{" "}
          <span className="proj">/ {project?.name ?? "…"}</span>
        </div>
        <div className="seg mode-seg" role="tablist">
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
                <span className="dot" /> {counts[s]} {s}
              </span>
            ) : null,
          )}
          {!conn && <span style={{ color: "var(--failed)" }}>· reconnecting…</span>}
        </div>
        <span className="spacer" />
        <button className="btn" onClick={() => api.runStale()} disabled={!staleCount} title="Run every stale, new, or failed node">
          ▶ Run stale{staleCount ? ` (${staleCount})` : ""}
        </button>
        <button className="btn" onClick={() => setNewOpen(true)}>+ Node</button>
        <SignedInAs />
        <button className="btn ghost icon-btn" onClick={toggleTheme} title="Toggle theme">{dark ? "☀" : "☾"}</button>
      </header>

      <div className={mainCls}>
        {mode === "views" ? (
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
                    <h2>An empty graph</h2>
                    <p>
                      {aiOn ? "Describe a pipeline to the agent, or add a node by hand." : "Add your first node — a Python function or a SQL query."}
                    </p>
                    <button className="btn primary" onClick={() => setNewOpen(true)}>+ New node</button>
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
          <div className="agent-collapsed-rail" onClick={() => setAgentOpen(true)}>
            <span style={{ color: "var(--accent)" }}>✦</span> Agent
          </div>
        )}
      </div>

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
    </div>
  );
}
