import { forwardRef, Suspense, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, radiusVars, shadowVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { ChatComposer, ChatComposerDrawer, ChatLayout, ChatMessage, ChatMessageBubble, ChatMessageList, ChatSystemMessage, ChatToolCalls, type ChatToolCallItem } from "@astryxdesign/core/Chat";
import { Code } from "@astryxdesign/core/Code";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { List, ListItem } from "@astryxdesign/core/List";
import { Spinner } from "@astryxdesign/core/Spinner";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { Token } from "@astryxdesign/core/Token";
import { VStack } from "@astryxdesign/core/VStack";
import { API_BASE, api, apiUrl, streamAgent, type AgentChat, type AgentEvent, type GraphNode } from "./api";
import { chatKey, itemsOf, type Item } from "./chat";
import { AgentMcpDialog } from "./AgentMcp";
import { IconChevronRight, IconClock, IconFolder, IconPlug, IconPlus, IconSliders, IconSpark } from "./icons";
import { ago } from "./util";
import { Markdown } from "./ui/Markdown";

// How many saved chats a new chat lists.
const RECENT = 8;

// The chat this browser had open in this project, so a reload brings it back.
const CHAT_KEY = chatKey(API_BASE);

function keptChat(): string | null {
  try {
    return localStorage.getItem(CHAT_KEY);
  } catch {
    return null;
  }
}

function keepChat(id: string | null) {
  try {
    if (id) localStorage.setItem(CHAT_KEY, id);
    else localStorage.removeItem(CHAT_KEY);
  } catch {
    /* storage may be blocked: the chat just isn't brought back */
  }
}

export interface AgentHandle {
  send: (text: string) => void;
}

/** What the side panel shows: the agent's chat, the project's files, its code history or its settings. */
export type SideTab = "agent" | "files" | "history" | "settings";

function toolLabel(it: Extract<Item, { kind: "tool" }>): string {
  const i = it.input ?? {};
  switch (it.name) {
    case "list_nodes":
      return "looked at the graph";
    case "read_node":
      return `read ${i.name ?? ""}`;
    case "write_node":
      return `wrote ${i.name ?? "…"}${i.lines ? ` · ${i.lines} lines` : ""}`;
    case "edit_node":
      return `edited ${i.name ?? "…"}`;
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
    case "edit_file":
      return `edited ${i.path ?? "…"}`;
    case "delete_file":
      return `deleted ${i.path ?? ""}`;
    case "docs":
      return i.topic ? `read the docs: ${i.topic}` : i.query ? `searched the docs: ${i.query}` : "looked at the docs";
    case "version":
      return "checked the exuvia version";
    case "report_issue":
      return `drafted an issue report: ${String(i.title ?? "").slice(0, 80)}`;
    default: {
      // An outside MCP server's tool (exuvia/agent_mcp.py): <server>__<tool>.
      const cut = it.name.indexOf("__");
      return cut > 0 ? `${it.name.slice(0, cut)}: ${it.name.slice(cut + 2)}` : it.name;
    }
  }
}

/** A run of the agent's steps, one after the other, as Astryx's list of tool calls. */
type Tool = Extract<Item, { kind: "tool" }>;
type Block = Exclude<Item, Tool> | { kind: "tools"; tools: Tool[] };

function blocks(items: Item[]): Block[] {
  const out: Block[] = [];
  for (const it of items) {
    const last = out[out.length - 1];
    if (it.kind !== "tool") out.push(it);
    else if (last?.kind === "tools") last.tools.push(it);
    else out.push({ kind: "tools", tools: [it] });
  }
  return out;
}

/**
 * The side panel: the agent's chat, the project's files, its code history and
 * its settings, one tab each. On a narrow window (`sheet`) it is a sheet over
 * the main area.
 */
export const SidePanel = forwardRef<
  AgentHandle,
  {
    enabled: boolean;
    model: string;
    selected: GraphNode | null;
    hasNodes: boolean;
    onTouch: (node: string) => void;
    onSelect: (node: string) => void;
    onCollapse: () => void;
    /** Drawn as a sheet along the bottom of a narrow window. */
    sheet?: boolean;
    tab: SideTab;
    onTab: (tab: SideTab) => void;
    /** The Files, History and Settings tabs' contents. The chat stays mounted under them, so a reply keeps streaming. */
    files: ReactNode;
    history: ReactNode;
    settings: ReactNode;
    /** Beside the Settings tab's name: what waits there (a count of requests, a failed install). */
    settingsEnd?: ReactNode;
    /** Whether this page has the Agent tab: not for a viewer of a shared project (the agent changes it). */
    agent?: boolean;
  }
>(function SidePanel({ enabled, model, selected, hasNodes, onTouch, onSelect, onCollapse, sheet, tab, onTab, files, history, settings, settingsEnd, agent = true }, ref) {
  const [items, setItems] = useState<Item[]>([]);
  const [mcpOpen, setMcpOpen] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [useCtx, setUseCtx] = useState(true);
  const session = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  // The latest saved chats, listed under the suggestions of a new chat.
  const [recent, setRecent] = useState<AgentChat[]>([]);

  useEffect(() => setUseCtx(true), [selected?.name]);
  // How much of the chat was on screen when another tab took over: more since then shows a dot on the Agent tab.
  const seen = useRef(0);
  if (tab === "agent") seen.current = items.length;
  const news = tab !== "agent" && (busy || items.length > seen.current);

  const onEvent = (e: AgentEvent) => {
    switch (e.type) {
      case "session":
        session.current = e.session_id;
        keepChat(e.session_id);
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

  const send = async (text: string) => {
    const msg = text.trim();
    if (!msg || busy || !enabled) return;
    setInput("");
    setBusy(true);
    setThinking(true);
    setItems((xs) => [...xs, { kind: "user", text: msg }]);
    const ctrl = new AbortController();
    abort.current = ctrl;
    try {
      await streamAgent(
        { message: msg, session_id: session.current, selected: useCtx && selected ? selected.name : null },
        onEvent,
        ctrl.signal,
      );
    } catch (e) {
      if ((e as Error).name !== "AbortError") onEvent({ type: "error", error: (e as Error).message });
    } finally {
      // Unless another chat has been opened since, and is being followed.
      if (abort.current === ctrl) {
        setBusy(false);
        setThinking(false);
        abort.current = null;
      }
    }
  };

  useImperativeHandle(ref, () => ({ send }));

  /**
   * Follow a turn that is already running in conversation `id` (started
   * before a reload, or in another tab) through the project's event stream,
   * then redraw the conversation from what was saved once it ends.
   */
  const follow = (id: string) => {
    const ctrl = new AbortController();
    abort.current = ctrl;
    setBusy(true);
    setThinking(true);
    const es = new EventSource(apiUrl("/api/events"));
    const end = () => {
      es.close();
      if (abort.current === ctrl) abort.current = null;
      setBusy(false);
      setThinking(false);
    };
    ctrl.signal.addEventListener("abort", end);
    es.onmessage = (m) => {
      const e = JSON.parse(m.data);
      if (e.type !== "agent_event" || e.session_id !== id || ctrl.signal.aborted) return;
      if (e.event.type === "done") {
        end();
        api
          .agentChat(id)
          .then((t) => session.current === id && setItems(itemsOf(t.events)))
          .catch(() => {});
      } else onEvent(e.event as AgentEvent);
    };
  };

  /** Open a saved conversation: its transcript, and its turn if one is running. */
  const open = async (id: string) => {
    abort.current?.abort();
    session.current = id;
    keepChat(id);
    try {
      const t = await api.agentChat(id);
      if (session.current !== id) return;
      if (!t.events.length && !t.running) {
        // Deleted, or never saved: start afresh.
        session.current = null;
        keepChat(null);
        setItems([]);
        return;
      }
      const drawn = itemsOf(t.events);
      seen.current = drawn.length; // what was said before is not news on the Agent tab
      setItems(drawn);
      if (t.running) follow(id);
    } catch {
      if (session.current === id) setItems([]);
    }
  };

  // Bring back the chat this browser had open, once the agent is known to be on.
  const restored = useRef(false);
  useEffect(() => {
    if (!enabled || !agent || restored.current) return;
    restored.current = true;
    const id = keptChat();
    if (id) open(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, agent]);

  /** Stop the turn: on the server too, not only this tab's stream of it. */
  const stop = () => {
    if (session.current) api.cancelAgent(session.current).catch(() => {});
    abort.current?.abort();
  };

  /** A new chat. The one before stays saved, under Recent chats. */
  const reset = () => {
    abort.current?.abort();
    session.current = null;
    keepChat(null);
    setItems([]);
  };

  // A new chat lists the ones before it, read again each time it is shown.
  const empty = items.length === 0;
  useEffect(() => {
    if (!enabled || !agent || tab !== "agent" || !empty) return;
    let live = true;
    api.agentChats(RECENT).then((xs) => live && setRecent(xs), () => {});
    return () => {
      live = false;
    };
  }, [enabled, agent, tab, empty]);

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

  /** A step that touched a node opens that node, from the step's detail. */
  const call = (it: Tool, i: number): ChatToolCallItem => ({
    key: it.id || String(i),
    name: toolLabel(it),
    status: it.ok === undefined ? "running" : it.ok ? "complete" : "error",
    node: it.node,
    errorMessage: it.ok === false ? it.summary : undefined,
    resultDetail:
      it.node || it.summary ? (
        <VStack gap={2} hAlign="start">
          {it.summary && (
            <Text type="code" color="secondary" wordBreak="break-word">
              {it.summary}
            </Text>
          )}
          {it.node && <Button size="sm" label={`Open ${it.node}`} onClick={() => onSelect(it.node!)} />}
        </VStack>
      ) : undefined,
  });

  return (
    <aside {...stylex.props(styles.panel, sheet && styles.sheet)} aria-label="Side panel">
      <div {...stylex.props(styles.head)}>
        <TabList role="tablist" value={tab} onChange={(t) => onTab(t as SideTab)} xstyle={styles.tabs}>
          {agent && (
            <Tab
              value="agent"
              label="Agent"
              icon={<IconSpark />}
              endContent={news ? <StatusDot variant="accent" isPulsing={busy} label={busy ? "The agent is working" : "New in the chat"} /> : undefined}
            />
          )}
          <Tab value="files" label="Files" icon={<IconFolder />} isLabelHidden={tab !== "files"} />
          <Tab value="history" label="History" icon={<IconClock />} isLabelHidden={tab !== "history"} />
          <Tab value="settings" label="Settings" icon={<IconSliders />} isLabelHidden={tab !== "settings"} endContent={settingsEnd} />
        </TabList>
        <span {...stylex.props(styles.grow)} />
        {tab === "agent" && agent && enabled && (
          <IconButton
            size="sm"
            variant="ghost"
            label="MCP servers"
            tooltip="MCP servers: outside tools for the agent"
            icon={<IconPlug />}
            onClick={() => setMcpOpen(true)}
          />
        )}
        {tab === "agent" && items.length > 0 && (
          <IconButton size="sm" variant="ghost" label="New chat" tooltip="New chat" icon={<IconPlus />} onClick={reset} />
        )}
        <IconButton
          size="sm"
          variant="ghost"
          label="Collapse the side panel"
          tooltip="Collapse the side panel"
          icon={<span {...stylex.props(sheet && styles.down)}><IconChevronRight /></span>}
          onClick={onCollapse}
        />
      </div>
      {mcpOpen && <AgentMcpDialog onClose={() => setMcpOpen(false)} />}
      {tab === "files" && files}
      {tab === "history" && history}
      {tab === "settings" && settings}
      {/* The chat stays mounted under the other tabs, so a reply keeps streaming. */}
      <div {...stylex.props(styles.body, (tab !== "agent" || !agent) && styles.hidden)}>
        {!enabled ? (
          <VStack gap={3} padding={4}>
            <Text>
              The agent builds, runs, and fixes nodes for you. To turn it on, set <Code>ANTHROPIC_API_KEY</Code> (Claude) or{" "}
              <Code>OPENROUTER_API_KEY</Code> (DeepSeek and other models via OpenRouter) in your environment or the project's{" "}
              <Code>.env</Code>, then restart <Code>exuvia serve</Code>. <Code>EXUVIA_MODEL</Code> picks the model. Through a Portkey
              gateway, set <Code>PORTKEY_API_KEY</Code> and <Code>EXUVIA_MODEL</Code>.
            </Text>
            <Text color="secondary">Everything else (editing, running, scheduling, querying) works without it.</Text>
          </VStack>
        ) : (
          <ChatLayout
            density="compact"
            composer={
              <ChatComposer
                density="compact"
                value={input}
                onChange={setInput}
                onSubmit={send}
                onStop={stop}
                isStopShown={busy}
                placeholder={hasNodes ? "Ask for a node, a fix, a chart, an analysis…" : "Describe the pipeline you want…"}
                drawer={
                  selected && useCtx ? (
                    <ChatComposerDrawer>
                      <HStack gap={1.5} vAlign="center">
                        <Text type="supporting" color="secondary">
                          Context
                        </Text>
                        <Token size="sm" color="blue" label={selected.name} onRemove={() => setUseCtx(false)} />
                      </HStack>
                    </ChatComposerDrawer>
                  ) : undefined
                }
                footerActions={
                  model ? (
                    <Text type="code" size="sm" color="secondary" maxLines={1}>
                      {model}
                    </Text>
                  ) : undefined
                }
              />
            }
            emptyState={
              <VStack gap={4} padding={3}>
                <VStack gap={1}>
                  <Text type="supporting" color="secondary">
                    Try asking
                  </Text>
                  <List density="compact" hasDividers>
                    {suggestions.map((s) => (
                      <ListItem key={s} label={<Text color="secondary">{s}</Text>} onClick={() => send(s)} />
                    ))}
                  </List>
                </VStack>
                {recent.length > 0 && (
                  <VStack gap={1}>
                    <Text type="supporting" color="secondary">
                      Recent chats
                    </Text>
                    <List density="compact" hasDividers>
                      {recent.map((c) => (
                        <ListItem
                          key={c.id}
                          itemKey={c.id}
                          label={c.title || "Untitled chat"}
                          description={ago(c.updated_at)}
                          endContent={c.running ? <StatusDot variant="accent" isPulsing label="The agent is working" /> : undefined}
                          onClick={() => open(c.id)}
                        />
                      ))}
                    </List>
                  </VStack>
                )}
              </VStack>
            }
          >
            {items.length > 0 && (
              <ChatMessageList density="compact" isStreaming={busy}>
                {blocks(items).map((b, i, all) => {
                  if (b.kind === "user")
                    return (
                      <ChatMessage key={i} sender="user">
                        <ChatMessageBubble>{b.text}</ChatMessageBubble>
                      </ChatMessage>
                    );
                  if (b.kind === "error") return <Banner key={i} status="error" title={b.text} />;
                  if (b.kind === "ai")
                    return (
                      <ChatMessage key={i} sender="assistant">
                        <ChatMessageBubble variant="ghost" width="100%">
                          <Suspense fallback={<Text>{b.text}</Text>}>
                            <Markdown density="compact" headingLevelStart={3} isStreaming={busy && i === all.length - 1}>
                              {b.text}
                            </Markdown>
                          </Suspense>
                        </ChatMessageBubble>
                      </ChatMessage>
                    );
                  return (
                    <ChatMessage key={i} sender="assistant">
                      <ChatMessageBubble variant="ghost" width="100%">
                        <ChatToolCalls calls={b.tools.map(call)} />
                      </ChatMessageBubble>
                    </ChatMessage>
                  );
                })}
                {thinking && busy && <ChatSystemMessage icon={<Spinner size="sm" />}>Thinking…</ChatSystemMessage>}
              </ChatMessageList>
            )}
          </ChatLayout>
        )}
      </div>
    </aside>
  );
});

/**
 * The collapsed side panel: its tabs as a rail of icons beside the main area,
 * or, on a narrow window (`bar`), as a bar of buttons along the bottom.
 */
export function SideRail(props: {
  agent: boolean;
  bar?: boolean;
  onOpen: (tab: SideTab) => void;
  /** Something waits in Settings (the agent's requests, a failed install): said in its button's name and by a dot on it. */
  settingsWaiting?: string;
}) {
  const waiting = props.settingsWaiting;
  const tabs: { tab: SideTab; label: string; tip: string; icon: ReactNode }[] = [
    ...(props.agent ? [{ tab: "agent" as const, label: "Agent", tip: "Open the agent", icon: <IconSpark /> }] : []),
    { tab: "files", label: "Files", tip: "Open the files of the graph and the views", icon: <IconFolder /> },
    { tab: "history", label: "History", tip: "Open the code's history", icon: <IconClock /> },
    {
      tab: "settings",
      label: waiting ? `Settings: ${waiting}` : "Settings",
      tip: waiting ? `Open the project's settings: ${waiting}` : "Open the project's packages, environment and storage",
      icon: <IconSliders />,
    },
  ];
  return (
    <div {...stylex.props(styles.rail, props.bar && styles.railBar)} role="group" aria-label="Side panel">
      {tabs.map((t) => (
        <span key={t.tab} {...stylex.props(styles.railItem)}>
          {props.bar ? (
            <Button variant="ghost" label={t.label} icon={t.icon} onClick={() => props.onOpen(t.tab)} />
          ) : (
            <IconButton variant="ghost" label={t.label} tooltip={t.tip} icon={t.icon} onClick={() => props.onOpen(t.tab)} />
          )}
          {t.tab === "settings" && waiting && !props.bar && (
            <span {...stylex.props(styles.railDot)} aria-hidden>
              <StatusDot variant="accent" label={waiting} />
            </span>
          )}
        </span>
      ))}
    </div>
  );
}

const styles = stylex.create({
  panel: {
    display: "flex",
    flexDirection: "column",
    minWidth: 0,
    minHeight: 0,
    backgroundColor: colorVars["--color-background-surface"],
    borderInlineStartWidth: "1px",
    borderInlineStartStyle: "solid",
    borderInlineStartColor: colorVars["--color-border"],
  },
  sheet: {
    position: "fixed",
    insetInline: 0,
    bottom: 0,
    zIndex: 40,
    height: "min(72vh, 620px)",
    borderInlineStartWidth: 0,
    borderTopWidth: "1px",
    borderTopStyle: "solid",
    borderTopColor: colorVars["--color-border-emphasized"],
    borderStartStartRadius: radiusVars["--radius-container"],
    borderStartEndRadius: radiusVars["--radius-container"],
    boxShadow: shadowVars["--shadow-high"],
  },
  // As tall as the project's bar beside it, so the two read as one band.
  head: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-1"],
    minHeight: "40px",
    flexShrink: 0,
    paddingInline: spacingVars["--spacing-2"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  tabs: { alignSelf: "flex-end", minWidth: 0 },
  grow: { flexGrow: 1 },
  down: { display: "block", transform: "rotate(90deg)" },
  body: { display: "flex", flexDirection: "column", flexGrow: 1, minHeight: 0 },
  hidden: { display: "none" },
  rail: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: spacingVars["--spacing-1"],
    paddingBlock: spacingVars["--spacing-2"],
    minHeight: 0,
    backgroundColor: colorVars["--color-background-surface"],
    borderInlineStartWidth: "1px",
    borderInlineStartStyle: "solid",
    borderInlineStartColor: colorVars["--color-border"],
  },
  railItem: { position: "relative", display: "inline-flex" },
  // The dot sits on the button's corner, out of the way of its icon; the button's name says the same in words.
  railDot: { position: "absolute", top: "2px", insetInlineEnd: "2px", display: "flex", pointerEvents: "none" },
  railBar: {
    flexDirection: "row",
    justifyContent: "space-around",
    paddingBlock: 0,
    borderInlineStartWidth: 0,
    borderTopWidth: "1px",
    borderTopStyle: "solid",
    borderTopColor: colorVars["--color-border"],
  },
});
