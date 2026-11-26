import type { GraphNode, NodeStage, ProjectView, State } from "./api";

/**
 * What the canvas draws, worked out before anything is laid out: the nodes,
 * the stages folded into one card, the views under what they read, and the
 * edges between them. Pure, so it is tested on its own (canvasGraph.test.ts).
 */

/** One thing on the canvas. Ids are a node's name, or `stage:<name>` / `view:<name>`. */
export type Item =
  | { kind: "node"; id: string; node: GraphNode }
  | { kind: "stage"; id: string; stage: NodeStage; members: GraphNode[] }
  | { kind: "view"; id: string; view: ProjectView }
  | { kind: "pinned"; id: string; view: ProjectView; width: number; height: number };

export type Link = { id: string; source: string; target: string; toView: boolean };

export interface Visible {
  items: Item[];
  links: Link[];
  /** Open stages with a node on the canvas: drawn as a frame around their nodes. */
  frames: { stage: NodeStage; members: string[] }[];
  /** Where a node is drawn: its own id, or its folded stage's. */
  at: Record<string, string>;
}

export const stageId = (name: string) => `stage:${name}`;
export const viewId = (name: string) => `view:${name}`;

export function visible(
  nodes: GraphNode[],
  views: ProjectView[],
  stages: NodeStage[],
  opts: { collapsed: ReadonlySet<string>; showViews: boolean },
): Visible {
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const members = new Map<string, GraphNode[]>();
  for (const g of stages) {
    const ms = g.nodes.map((n) => byName.get(n)).filter((n): n is GraphNode => !!n);
    if (ms.length) members.set(g.name, ms);
  }
  const at: Record<string, string> = {};
  const items: Item[] = [];
  const frames: Visible["frames"] = [];
  const folded = new Set<string>();
  for (const g of stages) {
    const ms = members.get(g.name);
    if (!ms) continue;
    if (opts.collapsed.has(g.name)) {
      folded.add(g.name);
      items.push({ kind: "stage", id: stageId(g.name), stage: g, members: ms });
      ms.forEach((m) => (at[m.name] = stageId(g.name)));
    } else frames.push({ stage: g, members: ms.map((m) => m.name) });
  }
  for (const n of nodes) {
    if (at[n.name]) continue;
    at[n.name] = n.name;
    items.push({ kind: "node", id: n.name, node: n });
  }
  const links: Link[] = [];
  const seen = new Set<string>();
  const link = (source: string | undefined, target: string, toView: boolean) => {
    if (!source || source === target) return;
    const id = `${source}->${target}`;
    if (seen.has(id)) return;
    seen.add(id);
    links.push({ id, source, target, toView });
  };
  for (const n of nodes) n.deps.forEach((d) => link(at[d], at[n.name], false));
  for (const v of views) {
    const pinned = v.canvas ?? null;
    if (!pinned && !opts.showViews) continue;
    const id = viewId(v.name);
    items.push(pinned ? { kind: "pinned", id, view: v, width: pinned.width, height: pinned.height } : { kind: "view", id, view: v });
    v.reads.forEach((r) => link(at[r], id, true));
  }
  return { items, links, frames, at };
}

/** How many of a set of nodes are in each state. */
export function countStates(nodes: GraphNode[]): Partial<Record<State, number>> {
  const out: Partial<Record<State, number>> = {};
  for (const n of nodes) out[n.state] = (out[n.state] ?? 0) + 1;
  return out;
}

/** The order a stage's states are told in, and the one that colours it: what most needs attention first. */
export const STATE_ORDER: State[] = ["failed", "error", "running", "queued", "stale", "new", "fresh"];

/** The state that speaks for a stage of nodes: the first of STATE_ORDER any of them is in. */
export function worstState(nodes: GraphNode[]): State | null {
  const counts = countStates(nodes);
  return STATE_ORDER.find((s) => counts[s]) ?? null;
}

/**
 * Everything upstream and downstream of `id` on the canvas, `id` included,
 * and the links on those paths: the data that flows into it and what it
 * flows on to.
 */
export function lineage(links: Link[], id: string): { items: Set<string>; links: Set<string> } {
  const up = new Map<string, Link[]>();
  const down = new Map<string, Link[]>();
  for (const l of links) {
    up.set(l.target, [...(up.get(l.target) ?? []), l]);
    down.set(l.source, [...(down.get(l.source) ?? []), l]);
  }
  const items = new Set([id]);
  const on = new Set<string>();
  const walk = (from: string, next: Map<string, Link[]>, end: (l: Link) => string) => {
    const todo = [from];
    const seen = new Set([from]);
    while (todo.length) {
      for (const l of next.get(todo.pop()!) ?? []) {
        on.add(l.id);
        const e = end(l);
        items.add(e);
        if (!seen.has(e)) {
          seen.add(e);
          todo.push(e);
        }
      }
    }
  };
  walk(id, up, (l) => l.source);
  walk(id, down, (l) => l.target);
  return { items, links: on };
}

export interface Activity {
  /** Running first, then queued, longest-waiting first. */
  running: GraphNode[];
  failed: GraphNode[];
  /** Scheduled and not paused, soonest first. */
  next: GraphNode[];
  /** Finished in the last hour, newest first. */
  recent: GraphNode[];
}

/** What the graph is doing now, for the activity panel. `now` is Unix seconds. */
export function activity(nodes: GraphNode[], now: number, limit = 5): Activity {
  const started = (n: GraphNode) => n.last_run?.started_at ?? n.last_run?.queued_at ?? now;
  const running = nodes
    .filter((n) => n.state === "running" || n.state === "queued")
    .sort((a, b) => Number(a.state !== "running") - Number(b.state !== "running") || started(a) - started(b));
  const failed = nodes.filter((n) => n.state === "failed" || n.state === "error");
  const next = nodes
    .filter((n) => n.schedule && !n.schedule.paused && n.schedule.next)
    .sort((a, b) => a.schedule!.next! - b.schedule!.next!)
    .slice(0, limit);
  const recent = nodes
    .filter((n) => n.finished_at && now - n.finished_at < 3600 && n.state !== "running" && n.state !== "queued")
    .sort((a, b) => b.finished_at! - a.finished_at!)
    .slice(0, limit);
  return { running, failed, next, recent };
}
