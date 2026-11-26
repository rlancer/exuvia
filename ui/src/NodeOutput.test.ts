import { describe, expect, it } from "vitest";
import type { GraphNode } from "./api";
import { outputPart } from "./NodeOutput";

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
