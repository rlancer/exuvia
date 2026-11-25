import { describe, expect, it } from "vitest";
import type { HubProject } from "./api";
import { RECENT_MAX, recentWork, sortProjects } from "./projectOrder";

const NOW = 1_000_000_000;
const DAY = 86400;

const p = (id: string, t: Partial<HubProject> = {}): HubProject => ({ id, name: "", nodes: 0, loaded: false, ...t });
const ids = (ps: HubProject[]) => ps.map((x) => x.id);

describe("ordering the hub's projects", () => {
  const list = [
    p("b", { name: "Beta", changed_at: NOW - 50 }),
    p("a", { name: "alpha", opened_at: NOW - 10, changed_at: NOW - 300 }),
    p("c", { name: "Gamma", edited_at: NOW - 100, opened_at: NOW - 200, changed_at: NOW - 100 }),
    p("d", { name: "Delta 10" }),
    p("e", { name: "Delta 9" }),
  ];

  it("puts what you used last first, then the rest by their last change", () => {
    expect(ids(sortProjects(list, "used"))).toEqual(["a", "c", "b", "e", "d"]);
  });

  it("puts what anyone changed last first", () => {
    expect(ids(sortProjects(list, "changed"))).toEqual(["b", "c", "a", "e", "d"]);
  });

  it("sorts names as people read them: no case, numbers by value", () => {
    expect(ids(sortProjects(list, "name"))).toEqual(["a", "b", "e", "d", "c"]);
  });

  it("names a project without one by its id", () => {
    expect(ids(sortProjects([p("zed"), p("x", { name: "Alpha" })], "name"))).toEqual(["x", "zed"]);
  });

  it("leaves the list it was given as it was", () => {
    const before = ids(list);
    sortProjects(list, "name");
    expect(ids(list)).toEqual(before);
  });
});

describe("recently worked on", () => {
  it("is what you changed lately, the latest first, opening alone not counting", () => {
    const list = [
      p("old", { edited_at: NOW - 40 * DAY }),
      p("seen", { opened_at: NOW - 1 }),
      p("x", { edited_at: NOW - 2 * DAY }),
      p("y", { edited_at: NOW - 60 }),
    ];
    expect(ids(recentWork(list, NOW))).toEqual(["y", "x"]);
  });

  it("shows a handful at most", () => {
    const many = Array.from({ length: 9 }, (_, i) => p(`p${i}`, { edited_at: NOW - i }));
    expect(ids(recentWork(many, NOW))).toEqual(ids(many.slice(0, RECENT_MAX)));
  });
});
