import { describe, expect, it } from "vitest";
import { chatKey, itemsOf } from "./chat";

describe("itemsOf", () => {
  it("redraws a saved conversation", () => {
    expect(
      itemsOf([
        { type: "user", text: "make a node", at: 1 },
        { type: "text", text: "On " },
        { type: "text", text: "it." },
        { type: "tool", id: "c1", name: "write_node", input: { name: "nums" } },
        { type: "tool_result", id: "c1", name: "write_node", ok: true, summary: "{}", node: "nums" },
        { type: "tool", id: "c2", name: "run_node", input: { name: "nums" } },
        { type: "error", error: "Stopped." },
        { type: "user", text: "again" },
      ]),
    ).toEqual([
      { kind: "user", text: "make a node" },
      { kind: "ai", text: "On it." },
      { kind: "tool", id: "c1", name: "write_node", input: { name: "nums" }, ok: true, summary: "{}", node: "nums" },
      { kind: "tool", id: "c2", name: "run_node", input: { name: "nums" } },
      { kind: "error", text: "Stopped." },
      { kind: "user", text: "again" },
    ]);
  });

  it("keeps a result whose call it never saw, and skips unknown events", () => {
    expect(
      itemsOf([
        { type: "tool_result", id: "x", name: "read_node", ok: false, summary: "not found" },
        { type: "thinking" } as never,
      ]),
    ).toEqual([{ kind: "tool", id: "x", name: "read_node", ok: false, summary: "not found", node: undefined }]);
  });
});

it("keeps one chat per project", () => {
  expect(chatKey("/api")).not.toEqual(chatKey("/p/a/api"));
});
