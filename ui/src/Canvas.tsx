import { lazy, memo, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  BackgroundVariant,
  ControlButton,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  useEdgesState,
  useNodesInitialized,
  useNodesState,
  useReactFlow,
  type Edge,
  type FitViewOptions,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import dagre from "@dagrejs/dagre";
import * as stylex from "@stylexjs/stylex";
import { colorVars, durationVars, fontWeightVars, radiusVars, shadowVars, spacingVars, textSizeVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Badge } from "@astryxdesign/core/Badge";
import { IconButton } from "@astryxdesign/core/IconButton";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import type { GraphNode, NodeStage, Project, ProjectView } from "./api";
import { ago, fmtNum, summary, until } from "./util";
import { IconClock, IconCollapse, IconLayers, IconUncollapse, IconWindow } from "./icons";
import { StateDot, statusOf, statusText } from "./ui/state";
import { countStates, lineage, STATE_ORDER, visible, worstState, type Item, type Visible } from "./canvasGraph";
import { ActivityPanel } from "./CanvasActivity";

const PinnedView = lazy(() => import("./PinnedView"));

const W = 232;
const H = 92;
const STAGE_H = 100;
const VIEW_H = 58;
/** A pinned view's header, above the size its manifest names. */
const PIN_HEAD = 38;
/** Room a stage's frame leaves around its nodes, and its header above them. */
const PAD = 12;
const FRAME_HEAD = 30;

type CardData = { g: GraphNode; selected: boolean; flash: number; dim: boolean };

/** A node of the graph: its state (stripe, dot, word), its name and kind, what it is for and what it last made. */
const Card = memo(function Card({ data }: NodeProps<Node<CardData>>) {
  const { g, selected, flash, dim } = data;
  const busy = g.state === "running" || g.state === "queued";
  const status = statusOf(g.state);
  return (
    <div key={flash} {...stylex.props(styles.card, selected && styles.selected, flash > 0 && styles.flash, dim && styles.dim)}>
      <span {...stylex.props(styles.stripe, stripe[status])} />
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div {...stylex.props(styles.row1)}>
        <StateDot state={g.state} />
        <span {...stylex.props(styles.name)} title={g.name}>{g.name}</span>
        <span {...stylex.props(styles.kind)}>
          <Badge label={g.kind === "python" ? "py" : "sql"} />
        </span>
      </div>
      {g.description ? <div {...stylex.props(styles.desc)}>{summary(g.description)}</div> : null}
      <div {...stylex.props(styles.meta)}>
        <span {...stylex.props(styles.state, statusText[status])}>{g.state}</span>
        {g.rows != null && <span>{fmtNum(g.rows)} rows</span>}
        {g.output_kind === "multi" ? (
          <span title={(g.schema ?? []).map((p) => p.name).join(", ")}>{g.schema?.length ?? 0} outputs</span>
        ) : (
          g.output_kind && g.output_kind !== "table" && <span>{g.output_kind}</span>
        )}
        {g.schedule && (
          <span {...stylex.props(styles.sched, g.schedule.paused && styles.paused)} title={`cron ${g.schedule.cron}`}>
            <IconClock size={11} /> {g.schedule.paused ? "paused" : until(g.schedule.next) || g.schedule.cron}
          </span>
        )}
        {!g.schedule && g.finished_at && !busy && <span>{ago(g.finished_at)}</span>}
      </div>
      {busy && <div {...stylex.props(styles.bar)} />}
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
});

/** The count of each state among a stage's nodes, worst first: a dot and a number each. */
function StateCounts({ members }: { members: GraphNode[] }) {
  const counts = countStates(members);
  return (
    <>
      {STATE_ORDER.filter((s) => counts[s]).map((s) => (
        <span key={s} {...stylex.props(styles.count)} title={`${counts[s]} ${s}`}>
          <StateDot state={s} label={`${counts[s]} ${s}`} />
          {counts[s]}
        </span>
      ))}
    </>
  );
}

type StageData = { stage: NodeStage; members: GraphNode[]; flash: number; dim: boolean; selected: boolean };

/** A folded stage: one card that stands for its nodes, coloured by the one that most needs attention. Click to open it out. */
const Stage = memo(function Stage({ data }: NodeProps<Node<StageData>>) {
  const { stage, members, flash, dim, selected } = data;
  const worst = worstState(members);
  const busy = members.some((m) => m.state === "running" || m.state === "queued");
  return (
    <div key={flash} {...stylex.props(styles.card, styles.stage, selected && styles.selected, flash > 0 && styles.flash, dim && styles.dim)} title="Open the stage out">
      <span {...stylex.props(styles.stripe, stripe[statusOf(worst)])} />
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div {...stylex.props(styles.row1)}>
        <IconLayers />
        <span {...stylex.props(styles.name, styles.stageName)} title={stage.title}>{stage.title}</span>
        <span {...stylex.props(styles.kind)}>
          <Badge label={`${members.length} nodes`} />
        </span>
      </div>
      {stage.description ? <div {...stylex.props(styles.desc)}>{stage.description}</div> : null}
      <div {...stylex.props(styles.meta)}>
        <StateCounts members={members} />
      </div>
      {busy && <div {...stylex.props(styles.bar)} />}
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
});

type FrameData = { stage: NodeStage; members: GraphNode[]; onCollapse: () => void };

/** An open stage: a labelled region behind its nodes, with what they are doing and a button to fold them into one card. */
const Frame = memo(function Frame({ data }: NodeProps<Node<FrameData>>) {
  const { stage, members } = data;
  return (
    <div {...stylex.props(styles.frame)}>
      <div {...stylex.props(styles.frameHead)}>
        <IconLayers />
        <span {...stylex.props(styles.frameTitle)}>{stage.title}</span>
        {stage.description ? <span {...stylex.props(styles.frameDesc)} title={stage.description}>{stage.description}</span> : <span {...stylex.props(styles.grow)} />}
        <span {...stylex.props(styles.frameCounts)}>
          <StateCounts members={members} />
        </span>
        <IconButton size="sm" variant="ghost" label={`Collapse ${stage.title}`} tooltip="Fold the stage into one card" icon={<IconCollapse />} onClick={data.onCollapse} />
      </div>
    </div>
  );
});

type ViewData = { view: ProjectView; dim: boolean };

/** A view, under the nodes it reads: what it is called and whether what it shows is current. Click to open it. */
const ViewCard = memo(function ViewCard({ data }: NodeProps<Node<ViewData>>) {
  const { view, dim } = data;
  const status = statusOf(view.state);
  return (
    <div {...stylex.props(styles.card, styles.viewCard, dim && styles.dim)} title={view.reason ?? view.description ?? undefined}>
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div {...stylex.props(styles.row1)}>
        <IconWindow />
        <span {...stylex.props(styles.name, styles.viewName)} title={view.title || view.name}>{view.title || view.name}</span>
      </div>
      <div {...stylex.props(styles.meta)}>
        <StateDot state={view.state} />
        <span {...stylex.props(styles.state, statusText[status])}>{view.state}</span>
        <span>view · reads {view.reads.length}</span>
      </div>
    </div>
  );
});

type PinnedData = { view: ProjectView; project: Project; width: number; height: number; dark: boolean; dim: boolean; onOpen: () => void };

/** A view pinned on the canvas, rendered live (PinnedView). */
const Pinned = memo(function Pinned({ data }: NodeProps<Node<PinnedData>>) {
  return (
    <div {...stylex.props(data.dim && styles.dim)}>
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <Suspense fallback={<div {...stylex.props(styles.card, styles.pinnedWait, styles.size(data.width, data.height + PIN_HEAD))} />}>
        <PinnedView view={data.view} project={data.project} width={data.width} height={data.height} dark={data.dark} onOpen={data.onOpen} />
      </Suspense>
    </div>
  );
});

const nodeTypes = { card: Card, stage: Stage, frame: Frame, view: ViewCard, pinned: Pinned };

function sizeOf(item: Item): { width: number; height: number } {
  switch (item.kind) {
    case "node":
      return { width: W, height: H };
    case "stage":
      return { width: W, height: STAGE_H };
    case "view":
      return { width: W, height: VIEW_H };
    case "pinned":
      return { width: item.width + 2, height: item.height + PIN_HEAD };
  }
}

/** Top-to-bottom layout; an open stage's nodes are kept together (a dagre cluster), with room for its frame. */
function layout(v: Visible): Record<string, { x: number; y: number }> {
  const framed = v.frames.length > 0;
  const g = new dagre.graphlib.Graph({ compound: framed });
  // A cluster spreads its ranks (dagre gives each one border ranks of its
  // own), so a framed graph asks for less space between them.
  g.setGraph({ rankdir: "TB", nodesep: framed ? 44 : 28, ranksep: framed ? 40 : 46, marginx: 20, marginy: 20 });
  g.setDefaultEdgeLabel(() => ({}));
  v.items.forEach((it) => g.setNode(it.id, sizeOf(it)));
  for (const f of v.frames) {
    g.setNode(`frame:${f.stage.name}`, {});
    f.members.forEach((m) => g.setParent(m, `frame:${f.stage.name}`));
  }
  v.links.forEach((l) => g.setEdge(l.source, l.target));
  dagre.layout(g);
  const out: Record<string, { x: number; y: number }> = {};
  v.items.forEach((it) => {
    const p = g.node(it.id);
    const s = sizeOf(it);
    out[it.id] = { x: p.x - s.width / 2, y: p.y - s.height / 2 };
  });
  return out;
}

/** What the canvas remembers per project in this browser: folded stages, whether views show, whether Activity is open. */
type Prefs = { collapsed: string[]; views: boolean; activity: boolean | null };

function usePrefs(project: string): [Prefs, (p: Partial<Prefs>) => void] {
  const key = `exuvia-canvas:${project}`;
  const read = useCallback((): Prefs => {
    const empty: Prefs = { collapsed: [], views: true, activity: null };
    try {
      return { ...empty, ...(JSON.parse(localStorage.getItem(key) ?? "{}") as Partial<Prefs>) };
    } catch {
      return empty;
    }
  }, [key]);
  const [prefs, setPrefs] = useState<Prefs>(read);
  useEffect(() => setPrefs(read()), [read]);
  const update = useCallback(
    (p: Partial<Prefs>) =>
      setPrefs((old) => {
        const next = { ...old, ...p };
        try {
          localStorage.setItem(key, JSON.stringify(next));
        } catch {
          /* private mode */
        }
        return next;
      }),
    [key],
  );
  return [prefs, update];
}

/**
 * The graph. Nodes are cards; a stage of nodes (exuvia.json `stages`) is a
 * labelled frame around them that folds into one card; each view is a card
 * under the nodes it reads, or the view itself when it is pinned
 * (`canvas` in its view.json). Selecting a node lights up what flows into it
 * and what it flows on to. Activity says what the graph is doing now.
 */
export function Canvas(props: {
  project: Project;
  selected: string | null;
  flash: Record<string, number>;
  dark: boolean;
  onSelect: (name: string | null) => void;
  onOpenView: (name: string) => void;
}) {
  const { project, selected, flash, dark, onSelect, onOpenView } = props;
  const nodes = project.nodes;
  const views = project.views;
  const stages = useMemo(() => project.stages ?? [], [project.stages]);
  const [prefs, setPrefs] = usePrefs(project.name);
  const collapsed = useMemo(() => new Set(prefs.collapsed), [prefs.collapsed]);
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<Node>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const { fitView } = useReactFlow();
  const dragged = useRef<Record<string, { x: number; y: number }>>({});
  const [moved, setMoved] = useState(0);
  // The node to bring into view at the next fit: at first the one selected, so coming back from the notebook keeps it in view.
  const focusing = useRef<string | null>(selected);
  // Open by default on a graph big enough to lose track of, where there is room for it.
  const narrow = useMediaQuery("(max-width: 1100px)");
  const activityOpen = prefs.activity ?? (nodes.length > 8 && !narrow);
  // Fit the graph (or the node brought into view) beside the open Activity panel, not under it.
  const fitPadding = useRef<FitViewOptions["padding"]>(0.15);
  fitPadding.current = activityOpen && !narrow ? { top: "32px", right: "340px", bottom: "32px", left: "32px" } : 0.15;
  const focusPadding = useRef<FitViewOptions["padding"]>(0.8);
  focusPadding.current = activityOpen && !narrow ? { top: "25%", right: "360px", bottom: "25%", left: "25%" } : 0.8;

  const shown = useMemo(() => visible(nodes, views, stages, { collapsed, showViews: prefs.views }), [nodes, views, stages, collapsed, prefs.views]);
  const at = useRef(shown.at);
  at.current = shown.at;

  // Re-layout only when what is drawn changes shape, so live status updates
  // don't make nodes jump around.
  const structure = useMemo(
    () =>
      [
        shown.items.map((i) => (i.kind === "pinned" ? `${i.id}@${i.width}x${i.height}` : i.id)).join(","),
        shown.links.map((l) => l.id).join(","),
        shown.frames.map((f) => `${f.stage.name}[${f.members.join(",")}]`).join(","),
      ].join("|"),
    [shown],
  );
  const positions = useMemo(() => {
    dragged.current = {};
    return layout(shown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structure]);

  const fold = useCallback(
    (name: string, on: boolean) => setPrefs({ collapsed: on ? [...new Set([...prefs.collapsed, name])] : prefs.collapsed.filter((g) => g !== name) }),
    [prefs.collapsed, setPrefs],
  );

  useEffect(() => {
    const byName = new Map(nodes.map((n) => [n.name, n]));
    const lit = selected && shown.at[selected] ? lineage(shown.links, shown.at[selected]) : null;
    const dim = (id: string) => !!lit && !lit.items.has(id);
    const pos = (id: string) => dragged.current[id] ?? positions[id] ?? { x: 0, y: 0 };
    const flashOf = (it: Item) => (it.kind === "stage" ? Math.max(0, ...it.members.map((m) => flash[m.name] ?? 0)) : 0);

    // Frames first, behind everything: each fits its nodes where they are now.
    const frames: Node[] = shown.frames.map((f) => {
      const ps = f.members.map(pos);
      const x0 = Math.min(...ps.map((p) => p.x)) - PAD;
      const y0 = Math.min(...ps.map((p) => p.y)) - PAD - FRAME_HEAD;
      const x1 = Math.max(...ps.map((p) => p.x + W)) + PAD;
      const y1 = Math.max(...ps.map((p) => p.y + H)) + PAD;
      return {
        id: `frame:${f.stage.name}`,
        type: "frame",
        position: { x: x0, y: y0 },
        width: x1 - x0,
        height: y1 - y0,
        zIndex: -1,
        draggable: false,
        selectable: false,
        focusable: false,
        data: { stage: f.stage, members: f.members.map((m) => byName.get(m)!), onCollapse: () => fold(f.stage.name, true) },
      };
    });
    const items: Node[] = shown.items.map((it) => {
      const base = { id: it.id, position: pos(it.id) };
      switch (it.kind) {
        case "node":
          return { ...base, type: "card", data: { g: it.node, selected: it.id === selected, flash: flash[it.id] ?? 0, dim: dim(it.id) } };
        case "stage":
          return { ...base, type: "stage", data: { stage: it.stage, members: it.members, flash: flashOf(it), dim: dim(it.id), selected: !!selected && shown.at[selected] === it.id } };
        case "view":
          return { ...base, type: "view", data: { view: it.view, dim: dim(it.id) } };
        case "pinned":
          return {
            ...base,
            type: "pinned",
            dragHandle: "[data-pin-head]",
            data: { view: it.view, project, width: it.width, height: it.height, dark, dim: dim(it.id), onOpen: () => onOpenView(it.view.name) },
          };
      }
    });
    setRfNodes([...frames, ...items]);

    const busy = new Set<string>();
    for (const n of nodes) if (n.state === "running" || n.state === "queued") busy.add(shown.at[n.name]);
    setRfEdges(
      shown.links.map((l) => {
        const on = lit?.links.has(l.id);
        const running = busy.has(l.target);
        const stroke = on || running ? "var(--color-accent)" : undefined;
        return {
          id: l.id,
          source: l.source,
          target: l.target,
          animated: running,
          // An edge into a running node is drawn in the accent, as the node's bar is; so is the selected node's lineage.
          style: {
            ...(stroke ? { stroke } : {}),
            ...(on ? { strokeWidth: 2 } : {}),
            ...(l.toView ? { strokeDasharray: "5 4" } : {}),
            ...(lit && !on ? { opacity: 0.3 } : {}),
          },
        };
      }),
    );
  }, [shown, nodes, positions, selected, flash, dark, project, moved, fold, onOpenView, setRfNodes, setRfEdges]);

  // Fit once React Flow has measured the nodes, and again on structural change.
  const initialized = useNodesInitialized();
  useEffect(() => {
    if (!initialized) return;
    const t = setTimeout(() => {
      // A node in a folded stage is brought into view as its stage's card.
      const target = focusing.current ? (at.current[focusing.current] ?? null) : null;
      focusing.current = null;
      if (target) fitView({ nodes: [{ id: target }], padding: focusPadding.current, duration: 300, maxZoom: 1.1 });
      else fitView({ padding: fitPadding.current, duration: 250, maxZoom: 1.1 });
    }, 30);
    return () => clearTimeout(t);
  }, [structure, initialized, fitView]);

  /** Show one node: open its stage if it is folded, select it and bring it into view. */
  const focus = useCallback(
    (name: string) => {
      const g = nodes.find((n) => n.name === name)?.stage;
      onSelect(name);
      if (g && collapsed.has(g)) {
        focusing.current = name; // the fit after the re-layout goes to it
        fold(g, false);
      } else fitView({ nodes: [{ id: name }], padding: focusPadding.current, duration: 300, maxZoom: 1.1 });
    },
    [nodes, collapsed, fold, fitView, onSelect],
  );

  const staged = shown.frames.length + shown.items.filter((i) => i.kind === "stage").length;
  const allFolded = staged > 0 && shown.frames.length === 0;
  const plainViews = views.filter((v) => !v.canvas).length;

  return (
    <div {...stylex.props(styles.flow)}>
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, n) => {
          if (n.type === "card") onSelect(n.id);
          else if (n.type === "stage") fold(n.id.slice("stage:".length), false);
          else if (n.type === "view") onOpenView(n.id.slice("view:".length));
        }}
        onNodeDragStop={(_, n) => {
          dragged.current[n.id] = n.position;
          setMoved((m) => m + 1); // the frames fit around where it now is
        }}
        onPaneClick={() => onSelect(null)}
        nodesConnectable={false}
        minZoom={0.15}
        maxZoom={1.8}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1.4} />
        <Controls {...stylex.props(styles.controls)} showInteractive={false} position="bottom-left">
          {staged > 0 && (
            <ControlButton
              title={allFolded ? "Open every stage out" : "Fold every stage into one card"}
              aria-label={allFolded ? "Open every stage out" : "Fold every stage into one card"}
              onClick={() => setPrefs({ collapsed: allFolded ? [] : stages.map((g) => g.name) })}
            >
              {allFolded ? <IconUncollapse /> : <IconCollapse />}
            </ControlButton>
          )}
          {plainViews > 0 && (
            <ControlButton
              title={prefs.views ? "Hide the views" : "Show the views under the nodes they read"}
              aria-label={prefs.views ? "Hide the views" : "Show the views"}
              onClick={() => setPrefs({ views: !prefs.views })}
            >
              <IconWindow />
            </ControlButton>
          )}
        </Controls>
        {nodes.length > 8 && (
          <MiniMap
            {...stylex.props(styles.minimap)}
            pannable
            zoomable
            position="bottom-right"
            // A stage's frame is an outline on the map; what is in it is drawn.
            nodeColor={(n) => (n.type === "frame" ? "transparent" : "var(--xy-minimap-node-background-color)")}
            nodeStrokeColor={(n) => (n.type === "frame" ? "var(--xy-minimap-node-background-color)" : "transparent")}
            nodeStrokeWidth={4}
          />
        )}
        {nodes.length > 0 && <ActivityPanel nodes={nodes} isOpen={activityOpen} onOpenChange={(open) => setPrefs({ activity: open })} onFocus={focus} />}
      </ReactFlow>
    </div>
  );
}

const REDUCED = "@media (prefers-reduced-motion: reduce)";

/** A node the agent or a run just touched: a ring that spreads from it and fades. */
const flashRing = stylex.keyframes({
  "0%": { boxShadow: `0 0 0 0 color-mix(in srgb, ${colorVars["--color-accent"]} 70%, transparent)` },
  "100%": { boxShadow: "0 0 0 14px transparent" },
});

/** A running node's bar: a light that crosses it. */
const slide = stylex.keyframes({
  from: { backgroundPosition: "100% 0" },
  to: { backgroundPosition: "-100% 0" },
});

const styles = stylex.create({
  // React Flow in the theme's colours, through its own custom properties: the
  // canvas is the page's ground, its edges and handles are the theme's rules,
  // and its controls and minimap are panels on it.
  flow: {
    width: "100%",
    height: "100%",
    "--xy-background-color": colorVars["--color-background-body"],
    "--xy-background-pattern-color": "var(--exuvia-canvas-dot)",
    "--xy-edge-stroke": colorVars["--color-border-emphasized"],
    "--xy-edge-stroke-selected": colorVars["--color-accent"],
    "--xy-edge-stroke-width": "1.5",
    "--xy-handle-background-color": colorVars["--color-background-surface"],
    "--xy-handle-border-color": colorVars["--color-border-emphasized"],
    "--xy-controls-button-background-color": colorVars["--color-background-surface"],
    "--xy-controls-button-background-color-hover": colorVars["--color-background-muted"],
    "--xy-controls-button-color": colorVars["--color-icon-primary"],
    "--xy-controls-button-color-hover": colorVars["--color-icon-primary"],
    "--xy-controls-button-border-color": colorVars["--color-border"],
    "--xy-controls-box-shadow": shadowVars["--shadow-low"],
    "--xy-minimap-background-color": colorVars["--color-background-surface"],
    "--xy-minimap-mask-background-color": `color-mix(in srgb, ${colorVars["--color-background-body"]} 65%, transparent)`,
    "--xy-minimap-node-background-color": colorVars["--color-border-emphasized"],
  },
  controls: { borderRadius: radiusVars["--radius-element"], overflow: "hidden" },
  minimap: { borderRadius: radiusVars["--radius-element"], overflow: "hidden", boxShadow: shadowVars["--shadow-low"] },
  grow: { flexGrow: 1 },

  card: {
    position: "relative",
    width: W,
    overflow: "hidden",
    cursor: "pointer",
    lineHeight: 1.4,
    color: colorVars["--color-text-primary"],
    backgroundColor: colorVars["--color-background-card"],
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
    boxShadow: shadowVars["--shadow-low"],
    transitionProperty: "border-color, box-shadow, opacity",
    transitionDuration: durationVars["--duration-fast"],
  },
  selected: {
    borderColor: colorVars["--color-accent"],
    boxShadow: `0 0 0 3px color-mix(in srgb, ${colorVars["--color-accent"]} 25%, transparent), ${shadowVars["--shadow-low"]}`,
  },
  // Off the selected node's lineage: still there, out of the way.
  dim: { opacity: 0.38 },
  flash: {
    animationName: { default: flashRing, [REDUCED]: "none" },
    animationDuration: "1.6s",
    animationTimingFunction: "ease-out",
  },
  // A folded stage reads as a stack of cards: a second edge under it.
  stage: {
    borderColor: colorVars["--color-border-emphasized"],
    boxShadow: `0 4px 0 -1px ${colorVars["--color-background-card"]}, 0 4px 0 0 ${colorVars["--color-border-emphasized"]}, ${shadowVars["--shadow-low"]}`,
    overflow: "visible",
  },
  stageName: { fontFamily: "inherit" },
  // A view is not a step of the pipeline: a dashed edge, the surface's ground.
  viewCard: {
    borderStyle: "dashed",
    borderColor: colorVars["--color-border-emphasized"],
    backgroundColor: colorVars["--color-background-surface"],
    boxShadow: "none",
  },
  viewName: { fontFamily: "inherit", fontWeight: fontWeightVars["--font-weight-medium"] },
  pinnedWait: { cursor: "default" },
  size: (width: number, height: number) => ({ width, height }),
  // The state's colour down the card's leading edge.
  stripe: { position: "absolute", insetBlock: 0, insetInlineStart: 0, width: "3px", borderStartStartRadius: radiusVars["--radius-container"], borderEndStartRadius: radiusVars["--radius-container"] },
  row1: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    paddingBlockStart: spacingVars["--spacing-2"],
    paddingBlockEnd: spacingVars["--spacing-1"],
    paddingInlineStart: spacingVars["--spacing-3"],
    paddingInlineEnd: spacingVars["--spacing-2"],
    color: colorVars["--color-text-secondary"],
  },
  name: {
    flexGrow: 1,
    minWidth: 0,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    color: colorVars["--color-text-primary"],
    fontFamily: typographyVars["--font-family-code"],
    fontSize: textSizeVars["--font-size-base"],
    fontWeight: fontWeightVars["--font-weight-semibold"],
  },
  kind: { display: "inline-flex", flexShrink: 0 },
  desc: {
    display: "-webkit-box",
    WebkitBoxOrient: "vertical",
    WebkitLineClamp: 2,
    overflow: "hidden",
    paddingInlineStart: spacingVars["--spacing-3"],
    paddingInlineEnd: spacingVars["--spacing-2"],
    color: colorVars["--color-text-secondary"],
    fontSize: textSizeVars["--font-size-sm"],
  },
  meta: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    paddingBlockStart: spacingVars["--spacing-1-5"],
    paddingBlockEnd: spacingVars["--spacing-2"],
    paddingInlineStart: spacingVars["--spacing-3"],
    paddingInlineEnd: spacingVars["--spacing-2"],
    color: colorVars["--color-text-secondary"],
    fontSize: textSizeVars["--font-size-sm"],
    whiteSpace: "nowrap",
  },
  count: { display: "inline-flex", alignItems: "center", gap: spacingVars["--spacing-1"], fontVariantNumeric: "tabular-nums" },
  state: { fontWeight: fontWeightVars["--font-weight-semibold"] },
  sched: { display: "inline-flex", alignItems: "center", gap: spacingVars["--spacing-1"] },
  paused: { textDecorationLine: "line-through" },
  bar: {
    height: "2px",
    backgroundImage: `linear-gradient(90deg, transparent, ${colorVars["--color-accent"]}, transparent)`,
    backgroundSize: "200% 100%",
    animationName: { default: slide, [REDUCED]: "none" },
    animationDuration: "1.2s",
    animationTimingFunction: "linear",
    animationIterationCount: "infinite",
  },

  // An open stage: a tinted region with its name, what it is for and its states along the top.
  frame: {
    width: "100%",
    height: "100%",
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
    backgroundColor: `color-mix(in srgb, ${colorVars["--color-background-muted"]} 55%, transparent)`,
    cursor: "default",
  },
  frameHead: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    height: FRAME_HEAD,
    paddingInlineStart: spacingVars["--spacing-3"],
    paddingInlineEnd: spacingVars["--spacing-1"],
    color: colorVars["--color-text-secondary"],
    fontSize: textSizeVars["--font-size-sm"],
    whiteSpace: "nowrap",
  },
  frameTitle: { color: colorVars["--color-text-primary"], fontWeight: fontWeightVars["--font-weight-semibold"], fontSize: textSizeVars["--font-size-base"] },
  frameDesc: { flexGrow: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" },
  frameCounts: { display: "inline-flex", alignItems: "center", gap: spacingVars["--spacing-2"] },
});

/** The stripe's colour per status (ui/state.tsx): a node that never ran has the rule's grey. */
const stripe = stylex.create({
  success: { backgroundColor: colorVars["--color-success"] },
  warning: { backgroundColor: colorVars["--color-warning"] },
  error: { backgroundColor: colorVars["--color-error"] },
  accent: { backgroundColor: colorVars["--color-accent"] },
  neutral: { backgroundColor: colorVars["--color-border-emphasized"] },
});
