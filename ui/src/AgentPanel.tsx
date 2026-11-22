import { forwardRef, lazy, Suspense, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from "react";
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
import { api, streamAgent, type AgentEvent, type GraphNode } from "./api";
import { IconChevronRight, IconClock, IconFolder, IconPlus, IconSpark } from "./icons";

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

// The Markdown renderer is fetched with the agent's first reply: a chat nobody opens costs nothing.
const Markdown = lazy(() => import("@astryxdesign/core/Markdown").then((m) => ({ default: m.Markdown })));

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
 * The side panel: the agent's chat, the project's files and its code history,
 * one tab each. On a narrow window (`sheet`) it is a sheet over the main area.
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
    /** The Files and History tabs' contents. The chat stays mounted under them, so a reply keeps streaming. */
    files: ReactNode;
    history: ReactNode;
    /** Whether this page has the Agent tab: not for a viewer of a shared project (the agent changes it). */
    agent?: boolean;
  }
>(function SidePanel({ enabled, model, selected, hasNodes, onTouch, onSelect, onCollapse, sheet, tab, onTab, files, history, agent = true }, ref) {
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [useCtx, setUseCtx] = useState(true);
  const session = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => setUseCtx(true), [selected?.name]);
  // How much of the chat was on screen when another tab took over: more since then shows a dot on the Agent tab.
  const seen = useRef(0);
  if (tab === "agent") seen.current = items.length;
  const news = tab !== "agent" && (busy || items.length > seen.current);

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
          <Tab value="files" label="Files" icon={<IconFolder />} />
          <Tab value="history" label="History" icon={<IconClock />} />
        </TabList>
        <span {...stylex.props(styles.grow)} />
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
      {tab === "files" && files}
      {tab === "history" && history}
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
                onStop={() => abort.current?.abort()}
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
              <VStack gap={1} padding={3}>
                <Text type="supporting" color="secondary">
                  Try asking
                </Text>
                <List density="compact" hasDividers>
                  {suggestions.map((s) => (
                    <ListItem key={s} label={<Text color="secondary">{s}</Text>} onClick={() => send(s)} />
                  ))}
                </List>
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
export function SideRail(props: { agent: boolean; bar?: boolean; onOpen: (tab: SideTab) => void }) {
  const tabs: { tab: SideTab; label: string; tip: string; icon: ReactNode }[] = [
    ...(props.agent ? [{ tab: "agent" as const, label: "Agent", tip: "Open the agent", icon: <IconSpark /> }] : []),
    { tab: "files", label: "Files", tip: "Open the files of the graph and the views", icon: <IconFolder /> },
    { tab: "history", label: "History", tip: "Open the code's history", icon: <IconClock /> },
  ];
  return (
    <div {...stylex.props(styles.rail, props.bar && styles.railBar)} role="group" aria-label="Side panel">
      {tabs.map((t) =>
        props.bar ? (
          <Button key={t.tab} variant="ghost" label={t.label} icon={t.icon} onClick={() => props.onOpen(t.tab)} />
        ) : (
          <IconButton key={t.tab} variant="ghost" label={t.label} tooltip={t.tip} icon={t.icon} onClick={() => props.onOpen(t.tab)} />
        ),
      )}
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
