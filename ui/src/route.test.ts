import { describe, expect, it } from "vitest";
import { format, landing, parse, routeOf, type Route } from "./route";

const ROUTES: Route[] = [
  { page: "graph" },
  { page: "notebook", node: null },
  { page: "notebook", node: "rolling_vol" },
  { page: "views", view: null },
  { page: "views", view: "vol_board" },
  { page: "history" },
  { page: "schedule" },
  { page: "file", path: "nodes/prices.py" },
  { page: "file", path: "views/board/App.tsx" },
  { page: "file", path: "lib/my helpers/données.py" },
  { page: "env", name: null },
  { page: "env", name: "OPENAI_API_KEY" },
];

describe("route", () => {
  it("reads back every address it writes", () => {
    for (const r of ROUTES) expect(parse(format(r)), format(r)).toEqual(r);
  });

  it("writes back every address it reads, as written", () => {
    for (const h of ["#notebook", "#notebook/prices", "#views", "#views/board", "#history", "#schedule", "#files/nodes/a.sql", "#files/lib/x%20y.py", "#env", "#env=TOKEN"])
      expect(format(parse(h))).toBe(h);
  });

  it("takes anything else as the graph", () => {
    for (const h of ["", "#", "#nope", "#notebook/Bad-Name", "#views/a/b", "#files/%E0%A4%A", "#env=1X", "#files/"]) expect(parse(h)).toEqual({ page: "graph" });
    expect(format({ page: "graph" })).toBe("");
  });

  it("accepts an address with or without its #", () => {
    expect(parse("notebook/a")).toEqual({ page: "notebook", node: "a" });
  });
});

describe("landing", () => {
  it("opens a file where it lives", () => {
    expect(landing(parse("#files/nodes/prices.py"))).toEqual({ mode: "graph", node: "prices" });
    expect(landing(parse("#files/nodes/sector_summary.sql"))).toEqual({ mode: "graph", node: "sector_summary" });
    expect(landing(parse("#files/views/board/App.tsx"))).toEqual({ mode: "views", view: { view: "board", file: "App.tsx" } });
    expect(landing(parse("#files/lib/util.py"))).toEqual({ mode: "helper", helper: "lib/util.py" });
    expect(landing(parse("#files/README.md"))).toEqual({ mode: "graph" });
  });

  it("opens the notebook on a cell, a view rendered, and the other pages", () => {
    expect(landing(parse("#notebook/vol"))).toEqual({ mode: "graph", layout: "notebook", node: "vol" });
    expect(landing(parse("#notebook"))).toEqual({ mode: "graph", layout: "notebook" });
    expect(landing(parse("#views/board"))).toEqual({ mode: "views", view: { view: "board" } });
    expect(landing(parse("#views"))).toEqual({ mode: "views" });
    expect(landing(parse("#history"))).toEqual({ mode: "history" });
    expect(landing(parse("#schedule"))).toEqual({ mode: "schedule" });
    expect(landing(parse("#env=X"))).toEqual({ mode: "graph" });
    expect(landing(parse(""))).toEqual({ mode: "graph" });
  });

  it("leaves each mode at its address", () => {
    expect(format(routeOf("views", null))).toBe("#views");
    expect(format(routeOf("history", null))).toBe("#history");
    expect(format(routeOf("schedule", null))).toBe("#schedule");
    expect(format(routeOf("helper", "lib/a b.py"))).toBe("#files/lib/a%20b.py");
    expect(format(routeOf("helper", null))).toBe("");
    expect(format(routeOf("graph", "lib/x.py"))).toBe("");
  });
});
