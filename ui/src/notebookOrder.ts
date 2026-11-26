import type { GraphNode, NodeStage, ProjectView } from "./api";

/**
 * The order the notebook reads the graph in, top to bottom. Pure, so it is
 * tested on its own (notebookOrder.test.ts).
 *
 * Reading order is not run order: a node runs when what it reads changes,
 * wherever it is on the page. The rules, in the order they win:
 *
 * 1. A node comes after every node it reads (when the graph has no cycle).
 * 2. Stages come in the order exuvia.json writes them, unless a stage reads
 *    from one written after it: then that one comes first. Within a stage,
 *    its nodes come in the order the stage lists them, except that a node's
 *    upstreams are pulled ahead of it.
 * 3. A node in no stage comes right after the last of its upstreams, and
 *    chains of such nodes are followed depth first, so `prices → returns →
 *    rolling_vol` read together instead of interleaving with unrelated
 *    branches layer by layer. One that reads from a stage comes after the
 *    whole stage, so the stage reads as one section.
 * 4. Adding a node never reorders the others: it goes after its last
 *    upstream, or where its stage lists it.
 */
export function readingOrder(nodes: GraphNode[], stages: NodeStage[]): string[] {
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const index = new Map(nodes.map((n, i) => [n.name, i]));
  // A node is in the first stage that names it (as the server says).
  const stageOf = new Map<string, number>();
  const members: string[][] = stages.map(() => []);
  stages.forEach((s, si) =>
    s.nodes.forEach((n) => {
      if (byName.has(n) && !stageOf.has(n)) {
        stageOf.set(n, si);
        members[si].push(n);
      }
    }),
  );
  const deps = (n: string) => (byName.get(n)?.deps ?? []).filter((d) => byName.has(d) && d !== n);
  const children = new Map<string, string[]>();
  for (const n of nodes) for (const d of deps(n.name)) children.set(d, [...(children.get(d) ?? []), n.name]);
  // Where a node would go if nothing pulled it: its stage and its place in it, then the project's order.
  const rank = (n: string): [number, number, number] => {
    const s = stageOf.get(n);
    return s === undefined ? [stages.length, 0, index.get(n)!] : [s, members[s].indexOf(n), index.get(n)!];
  };
  const byRank = (a: string, b: string) => {
    const ra = rank(a);
    const rb = rank(b);
    return ra[0] - rb[0] || ra[1] - rb[1] || ra[2] - rb[2];
  };

  const out: string[] = [];
  const done = new Set<string>();
  const onStack = new Set<string>();
  const emit = (n: string) => {
    done.add(n);
    out.push(n);
  };
  /** Place `n`, its upstreams first. A cycle is broken where it closes. */
  const visit = (n: string) => {
    if (done.has(n) || onStack.has(n)) return;
    onStack.add(n);
    [...deps(n)].sort(byRank).forEach(visit);
    onStack.delete(n);
    if (!done.has(n)) emit(n);
  };
  /** After `n`: each node in no stage that `n` was the last upstream of, and its own such chain, depth first. */
  const follow = (n: string) => {
    for (const c of children.get(n) ?? []) {
      if (done.has(c) || stageOf.has(c) || !deps(c).every((d) => done.has(d))) continue;
      emit(c);
      follow(c);
    }
  };

  for (const si of stageOrder(stages.length, members, deps, stageOf)) {
    const start = out.length;
    members[si].forEach(visit);
    // What reads from the stage (and from what it pulled in) and is in no stage comes after it.
    out.slice(start).forEach(follow);
  }
  // Nodes in no stage that read nothing in a stage: from each root, in the project's order.
  for (const n of nodes) {
    if (done.has(n.name) || deps(n.name).length) continue;
    visit(n.name);
    follow(n.name);
  }
  // What is left is on a cycle.
  for (const n of nodes) {
    if (done.has(n.name)) continue;
    visit(n.name);
    follow(n.name);
  }
  return out;
}

/**
 * The stages' order: as written, except that a stage comes after every stage
 * it reads from (directly, or through nodes in no stage). Stages that read
 * from each other keep the order written.
 */
function stageOrder(count: number, members: string[][], deps: (n: string) => string[], stageOf: Map<string, number>): number[] {
  const reads: Set<number>[] = members.map(() => new Set());
  members.forEach((ms, si) => {
    const seen = new Set<string>();
    const up = (n: string) => {
      for (const d of deps(n)) {
        if (seen.has(d)) continue;
        seen.add(d);
        const s = stageOf.get(d);
        if (s === undefined) up(d);
        else if (s !== si) reads[si].add(s);
      }
    };
    ms.forEach(up);
  });
  const order: number[] = [];
  const placed = new Set<number>();
  while (order.length < count) {
    const left = [...Array(count).keys()].filter((s) => !placed.has(s));
    const next = left.find((s) => [...reads[s]].every((r) => placed.has(r))) ?? left[0];
    placed.add(next);
    order.push(next);
  }
  return order;
}

/** One entry on the page: a node's cell, or a view's after the last node it reads. */
export type Cell = { kind: "node"; id: string; node: GraphNode } | { kind: "view"; id: string; view: ProjectView; pinned: boolean };

/** A run of cells under one heading: a stage, or nothing (nodes in no stage). */
export interface Section {
  key: string;
  stage: NodeStage | null;
  /** The stage's nodes, all of them, for its counts. */
  members: GraphNode[];
  /** A stage split by nodes it had to wait for: its second and later parts. */
  continued: boolean;
  cells: Cell[];
}

/**
 * The notebook: the nodes in reading order, cut into sections where the stage
 * changes, and each view right after the last node it reads (views that read
 * no node at the end).
 */
export function notebook(nodes: GraphNode[], views: ProjectView[], stages: NodeStage[]): Section[] {
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const order = readingOrder(nodes, stages);
  const at = new Map(order.map((n, i) => [n, i]));
  const stageOf = new Map<string, NodeStage>();
  for (const s of stages) for (const n of s.nodes) if (byName.has(n) && !stageOf.has(n)) stageOf.set(n, s);
  const after = new Map<string, ProjectView[]>();
  const tail: ProjectView[] = [];
  for (const v of views) {
    const last = v.reads.filter((r) => at.has(r)).sort((a, b) => at.get(b)! - at.get(a)!)[0];
    if (last) after.set(last, [...(after.get(last) ?? []), v]);
    else tail.push(v);
  }
  const viewCell = (v: ProjectView): Cell => ({ kind: "view", id: `view:${v.name}`, view: v, pinned: !!v.canvas });

  const sections: Section[] = [];
  const parts = new Map<string, number>();
  for (const name of order) {
    const node = byName.get(name)!;
    const stage = stageOf.get(name) ?? null;
    let cur = sections[sections.length - 1];
    if (!cur || cur.stage?.name !== stage?.name) {
      const key = stage ? `stage:${stage.name}` : "nostage";
      const n = parts.get(key) ?? 0;
      parts.set(key, n + 1);
      cur = {
        key: n ? `${key}:${n}` : key,
        stage,
        members: stage ? stage.nodes.map((m) => byName.get(m)).filter((m): m is GraphNode => !!m && stageOf.get(m.name) === stage) : [],
        continued: !!stage && n > 0,
        cells: [],
      };
      sections.push(cur);
    }
    cur.cells.push({ kind: "node", id: name, node });
    for (const v of after.get(name) ?? []) cur.cells.push(viewCell(v));
  }
  if (tail.length) {
    const last = sections[sections.length - 1];
    const cells = tail.map(viewCell);
    if (last && !last.stage) last.cells.push(...cells);
    else sections.push({ key: "views", stage: null, members: [], continued: false, cells });
  }
  // A section in no stage is told by what it holds.
  for (const s of sections) if (!s.stage) s.members = s.cells.flatMap((c) => (c.kind === "node" ? [c.node] : []));
  return sections;
}
