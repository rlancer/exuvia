import { describe, expect, it } from "vitest";
import type { GraphNode, NodeStage, ProjectView } from "./api";
import { activity, countStates, lineage, visible, worstState } from "./canvasGraph";

const node = (name: string, deps: string[] = [], extra: Partial<GraphNode> = {}): GraphNode =>
  ({ name, deps, state: "fresh", schedule: null, finished_at: null, last_run: null, ...extra }) as GraphNode;
const view = (name: string, reads: string[], canvas: ProjectView["canvas"] = null): ProjectView =>
  ({ name, reads, canvas, state: "ready" }) as ProjectView;
const stage = (name: string, nodes: string[]): NodeStage => ({ name, title: name, description: "", nodes, missing: [], problem: null });

// universe → prices → returns → vol; vol and universe → summary
const NODES = [
  node("universe"),
  node("prices", ["universe"]),
  node("returns", ["prices"]),
  node("vol", ["returns"]),
  node("summary", ["vol", "universe"]),
];
const STAGES = [stage("ingest", ["universe", "prices", "gone"]), stage("features", ["returns", "vol"]), stage("empty", ["gone"])];

describe("visible", () => {
  it("draws every node, frames open stages and leaves out empty ones", () => {
    const v = visible(NODES, [], STAGES, { collapsed: new Set(), showViews: true });
    expect(v.items.map((i) => i.id)).toEqual(["universe", "prices", "returns", "vol", "summary"]);
    expect(v.frames.map((f) => [f.stage.name, f.members])).toEqual([
      ["ingest", ["universe", "prices"]],
      ["features", ["returns", "vol"]],
    ]);
    expect(v.links.map((l) => l.id)).toEqual(["universe->prices", "prices->returns", "returns->vol", "vol->summary", "universe->summary"]);
  });

  it("folds a collapsed stage into one card and reroutes its edges, once each", () => {
    const v = visible(NODES, [view("board", ["vol", "returns", "summary"])], STAGES, { collapsed: new Set(["features", "empty"]), showViews: true });
    expect(v.items.map((i) => i.id)).toEqual(["stage:features", "universe", "prices", "summary", "view:board"]);
    expect(v.frames.map((f) => f.stage.name)).toEqual(["ingest"]);
    expect(v.at.vol).toBe("stage:features");
    expect(v.links.map((l) => l.id)).toEqual([
      "universe->prices",
      "prices->stage:features",
      "stage:features->summary",
      "universe->summary",
      "stage:features->view:board",
      "summary->view:board",
    ]);
    expect(v.links.filter((l) => l.toView).length).toBe(2);
  });

  it("hides plain views when asked, never pinned ones", () => {
    const views = [view("board", ["vol"]), view("overview", ["summary"], { width: 600, height: 400 })];
    const v = visible(NODES, views, [], { collapsed: new Set(), showViews: false });
    const pinned = v.items.filter((i) => i.kind === "pinned");
    expect(pinned.map((i) => i.id)).toEqual(["view:overview"]);
    expect(v.items.some((i) => i.id === "view:board")).toBe(false);
    expect(pinned[0]).toMatchObject({ width: 600, height: 400 });
  });
});

describe("states", () => {
  it("counts and picks what needs attention first", () => {
    const ns = [node("a"), node("b", [], { state: "stale" }), node("c", [], { state: "running" })];
    expect(countStates(ns)).toEqual({ fresh: 1, stale: 1, running: 1 });
    expect(worstState(ns)).toBe("running");
    expect(worstState([...ns, node("d", [], { state: "failed" })])).toBe("failed");
    expect(worstState([])).toBeNull();
  });
});

describe("lineage", () => {
  it("is everything upstream and downstream, and the links between", () => {
    const { links } = visible(NODES, [view("board", ["vol"])], [], { collapsed: new Set(), showViews: true });
    const l = lineage(links, "returns");
    expect([...l.items].sort()).toEqual(["prices", "returns", "summary", "universe", "view:board", "vol"]);
    // universe->summary carries none of returns' data.
    expect(l.links.has("universe->summary")).toBe(false);
    expect(l.links.has("vol->summary")).toBe(true);
  });
});

describe("activity", () => {
  it("lists what runs, what failed, what is next and what just finished", () => {
    const now = 10_000;
    const ns = [
      node("q", [], { state: "queued", last_run: { queued_at: now - 5 } as GraphNode["last_run"] }),
      node("r", [], { state: "running", last_run: { started_at: now - 30 } as GraphNode["last_run"] }),
      node("f", [], { state: "failed", finished_at: now - 60 }),
      node("s2", [], { schedule: { cron: "", paused: false, next: now + 200 } }),
      node("s1", [], { schedule: { cron: "", paused: false, next: now + 100 } }),
      node("p", [], { schedule: { cron: "", paused: true, next: now + 50 } }),
      node("old", [], { finished_at: now - 7200 }),
      node("new", [], { finished_at: now - 10 }),
    ];
    const a = activity(ns, now);
    expect(a.running.map((n) => n.name)).toEqual(["r", "q"]);
    expect(a.failed.map((n) => n.name)).toEqual(["f"]);
    expect(a.next.map((n) => n.name)).toEqual(["s1", "s2"]);
    expect(a.recent.map((n) => n.name)).toEqual(["new", "f"]);
  });
});
