import { describe, expect, it } from "vitest";
import type { GraphNode } from "./api";
import { outputPart, pct } from "./NodeOutput";

const node = (kind: GraphNode["output_kind"], parts: string[] = []): GraphNode =>
  ({ name: "n", output_kind: kind, schema: parts.map((name) => ({ name, type: "table" })) }) as GraphNode;

describe("outputPart", () => {
  it("shows no part of a node with one output", () => {
    expect(outputPart(node("table"), "x")).toEqual({ parts: [], shown: null });
  });

  it("shows the first output until one is picked", () => {
    expect(outputPart(node("multi", ["a", "b"]), null)).toEqual({ parts: ["a", "b"], shown: "a" });
    expect(outputPart(node("multi", ["a", "b"]), "b").shown).toBe("b");
  });

  it("goes back to the first when the picked one is gone", () => {
    expect(outputPart(node("multi", ["a", "c"]), "b").shown).toBe("a");
  });
});

describe("pct", () => {
  it("never rounds a share to 0% or 100% when it is neither", () => {
    expect(pct(0, 10)).toBe("0%");
    expect(pct(10, 10)).toBe("100%");
    expect(pct(1, 1000)).toBe("<1%");
    expect(pct(999, 1000)).toBe(">99%");
    expect(pct(5000, 250000)).toBe("2%");
    expect(pct(1, 0)).toBe("");
  });
});
