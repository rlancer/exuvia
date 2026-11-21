import { describe, expect, it } from "vitest";
import { PROJECT_ID } from "./api";
import { idFromName, toProjectId } from "./Hub";

describe("project ids from what is typed", () => {
  it("suggests an id from a name", () => {
    expect(idFromName("Market research")).toBe("market-research");
    expect(idFromName("  Q3 / Q4 numbers! ")).toBe("q3-q4-numbers");
    expect(idFromName("--draft")).toBe("draft");
    expect(idFromName("日本")).toBe("");
  });

  it("keeps the id field typeable: a space becomes - as it is typed", () => {
    expect(toProjectId("my ")).toBe("my-");
    expect(toProjectId("my r")).toBe("my-r");
    expect(toProjectId("-x")).toBe("x");
    expect(toProjectId("a".repeat(80))).toHaveLength(64);
  });

  it("only suggests ids the hub accepts", () => {
    for (const name of ["Market research", "a", "x".repeat(99), "Prices_2026", "émigré data"]) {
      const id = idFromName(name);
      if (id) expect(PROJECT_ID.test(id)).toBe(true);
    }
  });
});
