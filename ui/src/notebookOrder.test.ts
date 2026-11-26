import { describe, expect, it } from "vitest";
import type { GraphNode, NodeStage, ProjectView } from "./api";
import { notebook, readingOrder } from "./notebookOrder";

const node = (name: string, deps: string[] = []): GraphNode => ({ name, deps, state: "fresh" }) as GraphNode;
const stage = (name: string, nodes: string[]): NodeStage => ({ name, title: name, description: "", nodes, missing: [], problem: null });
const view = (name: string, reads: string[], pinned = false): ProjectView =>
  ({ name, reads, canvas: pinned ? { width: 400, height: 300 } : null }) as ProjectView;

/** Every node after all it reads. */
function respectsDeps(order: string[], nodes: GraphNode[]) {
  const at = new Map(order.map((n, i) => [n, i]));
  for (const n of nodes) for (const d of n.deps) if (at.has(d)) expect(at.get(d)!, `${d} before ${n.name}`).toBeLessThan(at.get(n.name)!);
  expect([...order].sort()).toEqual(nodes.map((n) => n.name).sort());
}

describe("readingOrder", () => {
  it("is topological", () => {
    const ns = [node("d", ["c"]), node("c", ["b", "a"]), node("b", ["a"]), node("a")];
    const o = readingOrder(ns, []);
    respectsDeps(o, ns);
    expect(o).toEqual(["a", "b", "c", "d"]);
  });

  it("follows chains depth first instead of layer by layer", () => {
    // Two branches off one root, listed so a breadth-first order would interleave them.
    const ns = [
      node("universe"),
      node("prices", ["universe"]),
      node("sectors", ["universe"]),
      node("returns", ["prices"]),
      node("sector_names", ["sectors"]),
      node("rolling_vol", ["returns"]),
    ];
    const o = readingOrder(ns, []);
    respectsDeps(o, ns);
    expect(o).toEqual(["universe", "prices", "returns", "rolling_vol", "sectors", "sector_names"]);
  });

  it("puts a join after the last of what it reads", () => {
    const ns = [node("a"), node("b"), node("ab", ["a", "b"]), node("a2", ["a"])];
    expect(readingOrder(ns, [])).toEqual(["a", "a2", "b", "ab"]);
  });

  it("keeps a stage's own order where dependencies allow", () => {
    const ns = [node("x"), node("y", ["x"]), node("z", ["x"]), node("w", ["y"])];
    // The stage lists z before y, and w (which needs y) first.
    const o = readingOrder(ns, [stage("s", ["x", "w", "z", "y"])]);
    respectsDeps(o, ns);
    expect(o).toEqual(["x", "y", "w", "z"]);
    expect(readingOrder(ns, [stage("s", ["x", "z", "y", "w"])])).toEqual(["x", "z", "y", "w"]);
  });

  it("orders stages as written unless one reads from a later one", () => {
    const ns = [node("raw"), node("clean", ["raw"]), node("report", ["clean"]), node("other")];
    expect(readingOrder(ns, [stage("ingest", ["raw", "clean"]), stage("reports", ["report", "other"])])).toEqual(["raw", "clean", "report", "other"]);
    // Written the wrong way round: the stage that is read comes first.
    const o = readingOrder(ns, [stage("reports", ["report", "other"]), stage("ingest", ["raw", "clean"])]);
    respectsDeps(o, ns);
    expect(o).toEqual(["raw", "clean", "report", "other"]);
  });

  it("sees through nodes in no stage when it orders stages", () => {
    const ns = [node("a"), node("glue", ["a"]), node("b", ["glue"])];
    const o = readingOrder(ns, [stage("late", ["b"]), stage("early", ["a"])]);
    expect(o).toEqual(["a", "glue", "b"]);
  });

  it("places nodes in no stage after the stage they read, not inside it", () => {
    const ns = [node("a"), node("b", ["a"]), node("loose", ["a"]), node("c", ["b"])];
    const o = readingOrder(ns, [stage("s", ["a", "b"]), stage("t", ["c"])]);
    expect(o).toEqual(["a", "b", "loose", "c"]);
  });

  it("pulls an upstream in no stage ahead of the stage that needs it", () => {
    const ns = [node("universe"), node("prices", ["universe"]), node("alone")];
    expect(readingOrder(ns, [stage("ingest", ["prices"])])).toEqual(["universe", "prices", "alone"]);
  });

  it("is stable: a new node goes after its last upstream and moves nothing else", () => {
    const base = [
      node("universe"),
      node("prices", ["universe"]),
      node("returns", ["prices"]),
      node("vol", ["returns"]),
      node("sectors", ["universe"]),
      node("summary", ["vol", "sectors"]),
    ];
    const stages = [stage("ingest", ["universe", "prices"]), stage("features", ["returns", "vol"])];
    const before = readingOrder(base, stages);
    for (const added of [node("aaa", ["prices"]), node("zzz", ["returns"]), node("join", ["universe", "vol"]), node("root")]) {
      const after = readingOrder([added, ...base], stages);
      expect(after.filter((n) => n !== added.name)).toEqual(before);
      const at = after.indexOf(added.name);
      if (added.deps.length) expect(at).toBeGreaterThan(Math.max(...added.deps.map((d) => after.indexOf(d))));
    }
    // Added to a stage, it goes where the stage lists it.
    const listed = readingOrder([...base, node("drawdown", ["returns"])], [stages[0], stage("features", ["returns", "drawdown", "vol"])]);
    expect(listed.filter((n) => n !== "drawdown")).toEqual(before);
    expect(listed.indexOf("drawdown")).toBe(listed.indexOf("returns") + 1);
  });

  it("survives a cycle and deps on nodes that do not exist", () => {
    const ns = [node("a", ["b"]), node("b", ["a"]), node("c", ["gone"])];
    expect([...readingOrder(ns, [])].sort()).toEqual(["a", "b", "c"]);
  });
});

describe("notebook", () => {
  const ns = [node("universe"), node("prices", ["universe"]), node("returns", ["prices"]), node("vol", ["returns"]), node("note", ["prices"])];
  const stages = [stage("ingest", ["universe", "prices", "gone"]), stage("features", ["returns", "vol"])];

  it("cuts sections where the stage changes", () => {
    const s = notebook(ns, [], stages);
    expect(s.map((x) => [x.key, x.cells.map((c) => c.id)])).toEqual([
      ["stage:ingest", ["universe", "prices"]],
      ["nostage", ["note"]],
      ["stage:features", ["returns", "vol"]],
    ]);
    expect(s[0].members.map((m) => m.name)).toEqual(["universe", "prices"]);
    expect(s[1].members.map((m) => m.name)).toEqual(["note"]);
  });

  it("puts each view after the last node it reads, and views that read nothing at the end", () => {
    const vs = [view("board", ["vol", "universe"], true), view("peek", ["prices"]), view("about", [])];
    const s = notebook(ns, vs, stages);
    const flat = s.flatMap((x) => x.cells.map((c) => c.id));
    expect(flat).toEqual(["universe", "prices", "view:peek", "note", "returns", "vol", "view:board", "view:about"]);
    const board = s.flatMap((x) => x.cells).find((c) => c.id === "view:board");
    expect(board).toMatchObject({ kind: "view", pinned: true });
    expect(s[s.length - 1].stage).toBeNull();
  });

  it("marks the second part of a stage split by what it waits for", () => {
    // s needs x2, which needs s's own a: s is split around x2.
    const nn = [node("a"), node("x2", ["a"]), node("b", ["x2"])];
    const s = notebook(nn, [], [stage("s", ["a", "b"]), stage("x", ["x2"])]);
    expect(s.map((x) => [x.key, x.continued])).toEqual([
      ["stage:s", false],
      ["stage:x", false],
      ["stage:s:1", true],
    ]);
  });
});
