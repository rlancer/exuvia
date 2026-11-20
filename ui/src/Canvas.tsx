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
import type { GraphNode } from "./api";
import { ago, fmtNum, until } from "./util";

const W = 232;
const H = 92;

type CardData = { g: GraphNode; selected: boolean; flash: number };

const Card = memo(function Card({ data }: NodeProps<Node<CardData>>) {
  const { g, selected, flash } = data;
  const busy = g.state === "running" || g.state === "queued";
  return (
    <div key={flash} className={`bnode s-${g.state} ${selected ? "selected" : ""} ${flash ? "flash" : ""}`}>
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div className="row1">
        <span className="dot" />
        <span className="name" title={g.name}>{g.name}</span>
        <span className={`kind ${g.kind}`}>{g.kind === "python" ? "py" : "sql"}</span>
      </div>
      {g.description ? <div className="desc">{g.description}</div> : null}
      <div className="meta">
        <span className="state">{g.state}</span>
        {g.rows != null && <span>{fmtNum(g.rows)} rows</span>}
        {g.output_kind && g.output_kind !== "table" && <span>{g.output_kind}</span>}
        {g.schedule && (
          <span className={`sched ${g.schedule.paused ? "paused" : ""}`} title={`cron ${g.schedule.cron}`}>
            ⏱ {g.schedule.paused ? "paused" : until(g.schedule.next) || g.schedule.cron}
          </span>
        )}
        {!g.schedule && g.finished_at && !busy && <span>{ago(g.finished_at)}</span>}
      </div>
      {busy && <div className="bar" />}
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
      <Background variant={BackgroundVariant.Dots} gap={18} size={1.4} color="var(--canvas-dot)" />
      <Controls showInteractive={false} position="bottom-left" />
      {nodes.length > 8 && <MiniMap pannable zoomable position="bottom-right" nodeColor="var(--line-strong)" />}
    </ReactFlow>
  );
}
