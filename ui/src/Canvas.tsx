import { memo, useEffect, useMemo, useRef } from "react";
import {
  Background,
  BackgroundVariant,
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
  type Node,
  type NodeProps,
} from "@xyflow/react";
import dagre from "@dagrejs/dagre";
import * as stylex from "@stylexjs/stylex";
import { colorVars, durationVars, fontWeightVars, radiusVars, shadowVars, spacingVars, textSizeVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Badge } from "@astryxdesign/core/Badge";
import type { GraphNode } from "./api";
import { ago, fmtNum, until } from "./util";
import { IconClock } from "./icons";
import { StateDot, statusOf, statusText } from "./ui/state";

const W = 232;
const H = 92;

type CardData = { g: GraphNode; selected: boolean; flash: number };

/** A node of the graph: its state (stripe, dot, word), its name and kind, what it is for and what it last made. */
const Card = memo(function Card({ data }: NodeProps<Node<CardData>>) {
  const { g, selected, flash } = data;
  const busy = g.state === "running" || g.state === "queued";
  const status = statusOf(g.state);
  return (
    <div key={flash} {...stylex.props(styles.card, selected && styles.selected, flash > 0 && styles.flash)}>
      <span {...stylex.props(styles.stripe, stripe[status])} />
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div {...stylex.props(styles.row1)}>
        <StateDot state={g.state} />
        <span {...stylex.props(styles.name)} title={g.name}>{g.name}</span>
        <Badge label={g.kind === "python" ? "py" : "sql"} />
      </div>
      {g.description ? <div {...stylex.props(styles.desc)}>{g.description}</div> : null}
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

const nodeTypes = { card: Card };

function layout(nodes: GraphNode[]): Record<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 28, ranksep: 46, marginx: 20, marginy: 20 });
  g.setDefaultEdgeLabel(() => ({}));
  const names = new Set(nodes.map((n) => n.name));
  nodes.forEach((n) => g.setNode(n.name, { width: W, height: H }));
  nodes.forEach((n) => n.deps.forEach((d) => names.has(d) && g.setEdge(d, n.name)));
  dagre.layout(g);
  const out: Record<string, { x: number; y: number }> = {};
  nodes.forEach((n) => {
    const p = g.node(n.name);
    out[n.name] = { x: p.x - W / 2, y: p.y - H / 2 };
  });
  return out;
}

export function Canvas(props: {
  nodes: GraphNode[];
  selected: string | null;
  flash: Record<string, number>;
  onSelect: (name: string | null) => void;
}) {
  const { nodes, selected, flash, onSelect } = props;
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<Node<CardData>>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const { fitView } = useReactFlow();
  const dragged = useRef<Record<string, { x: number; y: number }>>({});

  // Re-layout only when the graph's *structure* changes, so live status
  // updates don't make nodes jump around.
  const structure = useMemo(
    () => nodes.map((n) => `${n.name}<${n.deps.join(",")}`).sort().join("|"),
    [nodes],
  );
  const positions = useMemo(() => {
    dragged.current = {};
    return layout(nodes);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structure]);

  useEffect(() => {
    const names = new Set(nodes.map((n) => n.name));
    setRfNodes(
      nodes.map((g) => ({
        id: g.name,
        type: "card",
        position: dragged.current[g.name] ?? positions[g.name] ?? { x: 0, y: 0 },
        data: { g, selected: g.name === selected, flash: flash[g.name] ?? 0 },
      })),
    );
    const busy = new Set(nodes.filter((n) => n.state === "running" || n.state === "queued").map((n) => n.name));
    setRfEdges(
      nodes.flatMap((n) =>
        n.deps
          .filter((d) => names.has(d))
          .map((d) => ({
            id: `${d}->${n.name}`,
            source: d,
            target: n.name,
            animated: busy.has(n.name),
            // An edge into a running node is drawn in the accent, as the node's bar is.
            style: busy.has(n.name) ? { stroke: "var(--color-accent)" } : undefined,
          })),
      ),
    );
  }, [nodes, positions, selected, flash, setRfNodes, setRfEdges]);

  // Fit once React Flow has measured the nodes, and again on structural change.
  const initialized = useNodesInitialized();
  useEffect(() => {
    if (!initialized) return;
    const t = setTimeout(() => fitView({ padding: 0.15, duration: 250, maxZoom: 1.1 }), 20);
    return () => clearTimeout(t);
  }, [structure, initialized, fitView]);

  return (
    <div {...stylex.props(styles.flow)}>
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, n) => onSelect(n.id)}
        onNodeDragStop={(_, n) => (dragged.current[n.id] = n.position)}
        onPaneClick={() => onSelect(null)}
        nodesConnectable={false}
        minZoom={0.2}
        maxZoom={1.8}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1.4} />
        <Controls {...stylex.props(styles.controls)} showInteractive={false} position="bottom-left" />
        {nodes.length > 8 && <MiniMap {...stylex.props(styles.minimap)} pannable zoomable position="bottom-right" />}
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

  card: {
    position: "relative",
    width: W,
    overflow: "hidden",
    cursor: "pointer",
    color: colorVars["--color-text-primary"],
    backgroundColor: colorVars["--color-background-card"],
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
    boxShadow: shadowVars["--shadow-low"],
    transitionProperty: "border-color, box-shadow",
    transitionDuration: durationVars["--duration-fast"],
  },
  selected: {
    borderColor: colorVars["--color-accent"],
    boxShadow: `0 0 0 3px color-mix(in srgb, ${colorVars["--color-accent"]} 25%, transparent), ${shadowVars["--shadow-low"]}`,
  },
  flash: {
    animationName: { default: flashRing, [REDUCED]: "none" },
    animationDuration: "1.6s",
    animationTimingFunction: "ease-out",
  },
  // The state's colour down the card's leading edge.
  stripe: { position: "absolute", insetBlock: 0, insetInlineStart: 0, width: "3px" },
  row1: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    paddingBlockStart: spacingVars["--spacing-2"],
    paddingBlockEnd: spacingVars["--spacing-1"],
    paddingInlineStart: spacingVars["--spacing-3"],
    paddingInlineEnd: spacingVars["--spacing-2"],
  },
  name: {
    flexGrow: 1,
    minWidth: 0,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    fontFamily: typographyVars["--font-family-code"],
    fontSize: textSizeVars["--font-size-base"],
    fontWeight: fontWeightVars["--font-weight-semibold"],
  },
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
});

/** The stripe's colour per status (ui/state.tsx): a node that never ran has the rule's grey. */
const stripe = stylex.create({
  success: { backgroundColor: colorVars["--color-success"] },
  warning: { backgroundColor: colorVars["--color-warning"] },
  error: { backgroundColor: colorVars["--color-error"] },
  accent: { backgroundColor: colorVars["--color-accent"] },
  neutral: { backgroundColor: colorVars["--color-border-emphasized"] },
});
