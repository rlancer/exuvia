import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from "react";
import { api, streamAgent, type AgentEvent, type GraphNode } from "./api";
import { renderMarkdown } from "./util";
import { IconAlert, IconChevronRight, IconClock, IconFolder, IconSpark } from "./icons";

type Item =
  | { kind: "user"; text: string }
  | { kind: "ai"; text: string }
  | { kind: "tool"; id: string; name: string; input?: Record<string, unknown>; ok?: boolean; summary?: string; node?: string }
  | { kind: "error"; text: string };

export interface AgentHandle {
  send: (text: string) => void;
}

/** What the side panel shows: the agent's chat, the project's files or its code history. */
export type SideTab = "agent" | "files" | "history";

const ICONS: Record<string, string> = {
  list_nodes: "☰",
  read_node: "◇",
  write_node: "✎",
  delete_node: "✕",
  run_node: "▶",
  query_data: "⌕",
  list_files: "☰",
  read_file: "◇",
  write_file: "✎",
  delete_file: "✕",
};

function toolLabel(it: Extract<Item, { kind: "tool" }>): string {
  const i = it.input ?? {};
  switch (it.name) {
    case "list_nodes":
      return "looked at the graph";
    case "read_node":
      return `read ${i.name ?? ""}`;
    case "write_node":
      return `wrote ${i.name ?? "…"}${i.lines ? ` · ${i.lines} lines` : ""}`;
    case "delete_node":
      return `deleted ${i.name ?? ""}`;
    case "run_node": {
      let extra = "";
      if (it.summary) {
        try {
          const s = JSON.parse(it.summary);
          if (s.rows != null) extra = ` · ${Number(s.rows).toLocaleString()} rows`;
        } catch {
          /* summary may be truncated JSON */
          const m = it.summary.match(/"rows":\s*(\d+)/);
          if (m) extra = ` · ${Number(m[1]).toLocaleString()} rows`;
        }
      }
      return `ran ${i.name ?? ""}${it.ok === false ? " · failed" : extra}`;
    }
    case "query_data":
      return `query: ${String(i.sql ?? "").replace(/\s+/g, " ").slice(0, 80)}`;
    case "list_files":
      return "looked at the helper files";
    case "read_file":
      return `read ${i.path ?? ""}`;
    case "write_file":
      return `wrote ${i.path ?? "…"}${i.lines ? ` · ${i.lines} lines` : ""}`;
    case "delete_file":
      return `deleted ${i.path ?? ""}`;
    case "docs":
      return i.topic ? `read the docs: ${i.topic}` : i.query ? `searched the docs: ${i.query}` : "looked at the docs";
    case "report_issue":
      return `drafted an issue report: ${String(i.title ?? "").slice(0, 80)}`;
    default:
      return it.name;
  }
}

export const AgentPanel = forwardRef<
  AgentHandle,
  {
    enabled: boolean;
    model: string;
    selected: GraphNode | null;
    hasNodes: boolean;
    onTouch: (node: string) => void;
    onSelect: (node: string) => void;
    onCollapse: () => void;
    tab: SideTab;
    onTab: (tab: SideTab) => void;
    /** The Files and History tabs' contents. The chat stays mounted under them, so a reply keeps streaming. */
    files: ReactNode;
    history: ReactNode;
  }
>(function AgentPanel({ enabled, model, selected, hasNodes, onTouch, onSelect, onCollapse, tab, onTab, files, history }, ref) {
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [useCtx, setUseCtx] = useState(true);
  const session = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => setUseCtx(true), [selected?.name]);
  // How much of the chat was on screen when another tab took over: more since then shows a dot on the Agent tab.
  const seen = useRef(0);
  if (tab === "agent") seen.current = items.length;
  const news = tab !== "agent" && (busy || items.length > seen.current);
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [items, thinking]);

  const send = async (text: string) => {
    const msg = text.trim();
    if (!msg || busy || !enabled) return;
    setInput("");
    setBusy(true);
    setThinking(true);
    setItems((xs) => [...xs, { kind: "user", text: msg }]);
    const ctrl = new AbortController();
    abort.current = ctrl;
    const onEvent = (e: AgentEvent) => {
      switch (e.type) {
        case "session":
          session.current = e.session_id;
          break;
        case "thinking":
          setThinking(true);
          break;
        case "text":
          setThinking(false);
          setItems((xs) => {
            const last = xs[xs.length - 1];
            if (last?.kind === "ai") return [...xs.slice(0, -1), { ...last, text: last.text + e.text }];
            return [...xs, { kind: "ai", text: e.text }];
          });
          break;
        case "tool_start":
          setThinking(false);
          setItems((xs) => [...xs, { kind: "tool", id: e.id, name: e.name }]);
          break;
        case "tool":
          setItems((xs) => xs.map((x) => (x.kind === "tool" && x.id === e.id ? { ...x, input: e.input } : x)));
          setThinking(true);
          break;
        case "tool_result":
          if (e.node) onTouch(e.node);
          setItems((xs) =>
            xs.map((x) => (x.kind === "tool" && x.id === e.id ? { ...x, ok: e.ok, summary: e.summary, node: e.node } : x)),
          );
          break;
        case "error":
          setItems((xs) => [...xs, { kind: "error", text: e.error }]);
          break;
        case "done":
          break;
      }
    };
    try {
      await streamAgent(
        { message: msg, session_id: session.current, selected: useCtx && selected ? selected.name : null },
        onEvent,
        ctrl.signal,
      );
    } catch (e) {
      if ((e as Error).name !== "AbortError") onEvent({ type: "error", error: (e as Error).message });
    } finally {
      setBusy(false);
      setThinking(false);
      abort.current = null;
    }
  };

  useImperativeHandle(ref, () => ({ send }));

  const reset = () => {
    abort.current?.abort();
    if (session.current) api.resetAgent(session.current).catch(() => {});
    session.current = null;
    setItems([]);
  };

  const suggestions = !hasNodes
    ? [
        "Build a pipeline: daily prices for 10 large-cap tickers → returns → 60-day rolling correlation matrix",
        "Create a node that loads a CSV from data/ and profiles each column",
      ]
    : selected
      ? [
          `Explain what ${selected.name} does and what its output looks like`,
          `Add a chart to ${selected.name}`,
          `Build a node downstream of ${selected.name} that flags outliers`,
        ]
      : [
          "Summarize this pipeline and what the latest data shows",
          "Add a drawdown node per ticker with a chart",
          "Schedule the source nodes for every 30 minutes during market hours",
        ];

  return (
    <aside className="agent">
      <div className="agent-head side-head">
        <div className="side-tabs" role="tablist" aria-label="Side panel">
          <button role="tab" aria-selected={tab === "agent"} className={`side-tab ${tab === "agent" ? "on" : ""}`} onClick={() => onTab("agent")} title={enabled ? `The agent (${model})` : "The agent"}>
            <IconSpark /> Agent
            {news && <span className={`side-news ${busy ? "pulse" : ""}`} title={busy ? "The agent is working" : "New in the chat"} />}
          </button>
          <button role="tab" aria-selected={tab === "files"} className={`side-tab ${tab === "files" ? "on" : ""}`} onClick={() => onTab("files")} title="The files of the graph and the views">
            <IconFolder /> Files
          </button>
          <button role="tab" aria-selected={tab === "history"} className={`side-tab ${tab === "history" ? "on" : ""}`} onClick={() => onTab("history")} title="Every saved version of the code, and deleted nodes and views to bring back">
            <IconClock /> History
          </button>
        </div>
        <span className="spacer" />
        {tab === "agent" && items.length > 0 && <button className="btn sm ghost" onClick={reset}>New chat</button>}
        <button className="btn sm ghost icon-btn collapse" title="Collapse the side panel" aria-label="Collapse the side panel" onClick={onCollapse}>
          <IconChevronRight />
        </button>
      </div>
      {tab === "files" && files}
      {tab === "history" && history}
      <div className="side-body" hidden={tab !== "agent"}>
      {!enabled ? (
        <div className="agent-off">
          The agent builds, runs, and fixes nodes for you. To turn it on, set <code>ANTHROPIC_API_KEY</code> (Claude) or{" "}
          <code>OPENROUTER_API_KEY</code> (DeepSeek and other models via OpenRouter) in your environment or the
          project's <code>.env</code>, then restart <code>exuvia serve</code>. <code>EXUVIA_MODEL</code> picks the model.
          Through a Portkey gateway, set <code>PORTKEY_API_KEY</code> and <code>EXUVIA_MODEL</code>.
          <br />
          <br />
          Everything else (editing, running, scheduling, querying) works without it.
        </div>
      ) : (
        <>
          <div className="msgs" ref={scroller}>
            {items.length === 0 && (
              <div className="suggest">
                <div className="lead">Try asking</div>
                {suggestions.map((s) => (
                  <button key={s} onClick={() => send(s)}>{s}</button>
                ))}
              </div>
            )}
            {items.map((it, i) => {
              if (it.kind === "user") return <div key={i} className="msg user">{it.text}</div>;
              if (it.kind === "error")
                return (
                  <div key={i} className="msg err" role="alert">
                    <IconAlert size={13} />
                    <span>{it.text}</span>
                  </div>
                );
              if (it.kind === "ai")
                return <div key={i} className="msg ai" dangerouslySetInnerHTML={{ __html: renderMarkdown(it.text) }} />;
              const done = it.ok !== undefined;
              const body = (
                <>
                  <span className="ic">{done ? (it.ok ? ICONS[it.name] ?? "•" : "!") : <span className="dot pulse" style={{ background: "var(--running)" }} />}</span>
                  <span className="lbl">{toolLabel(it)}</span>
                </>
              );
              const cls = `tool ${it.node ? "link" : ""} ${done ? (it.ok ? "ok" : "bad") : ""}`;
              // A step that touched a node opens that node.
              return it.node ? (
                <button key={i} className={cls} title={it.summary ?? `Open ${it.node}`} onClick={() => onSelect(it.node!)}>{body}</button>
              ) : (
                <div key={i} className={cls} title={it.summary}>{body}</div>
              );
            })}
            {thinking && busy && (
              <div className="thinking" role="status"><span className="dot pulse" style={{ background: "var(--running)" }} /> thinking…</div>
            )}
          </div>
          <div className="composer">
            {selected && useCtx && (
              <span className="ctx-chip">
                context: {selected.name}
                <button onClick={() => setUseCtx(false)} title="Don't send the selected node" aria-label="Don't send the selected node">×</button>
              </span>
            )}
            <textarea
              value={input}
              aria-label="Message the agent"
              placeholder={hasNodes ? "Ask for a node, a fix, a chart, an analysis…" : "Describe the pipeline you want…"}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send(input);
                }
              }}
            />
            <div className="row">
              <span>Enter to send · Shift+Enter newline</span>
              {model && <span className="model" title="The agent's model">{model}</span>}
              <span className="spacer" />
              {busy ? (
                <button className="btn sm" onClick={() => abort.current?.abort()}>Stop</button>
              ) : (
                <button className="btn sm primary" onClick={() => send(input)} disabled={!input.trim()}>Send</button>
              )}
            </div>
          </div>
        </>
      )}
      </div>
    </aside>
  );
});
