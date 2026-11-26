import { describe, expect, it } from "vitest";
import { decode, encode, oneOf } from "./usePersisted";

const layout = oneOf("canvas", "notebook");

describe("stored values", () => {
  it("keeps a string as itself, and anything else as JSON", () => {
    expect(encode("open")).toBe("open");
    expect(encode({ collapsed: ["a"], views: true })).toBe('{"collapsed":["a"],"views":true}');
    expect(encode(false)).toBe("false");
  });

  it("reads back what it writes", () => {
    for (const v of ["notebook", { collapsed: ["ingest"], views: false, activity: null }, true, 3]) expect(decode(encode(v), (x) => x)).toEqual(v);
  });

  it("reads what earlier versions stored", () => {
    // exuvia-agent, exuvia-side and exuvia-graph-layout were plain strings; exuvia-canvas:<project> JSON.
    expect(decode("open", oneOf("open", "closed"))).toBe("open");
    expect(decode("files", oneOf("files", "history"))).toBe("files");
    expect(decode('{"collapsed":[],"views":true}', (v) => v as object)).toEqual({ collapsed: [], views: true });
  });

  it("reads nothing it does not accept", () => {
    expect(decode(null, layout)).toBeUndefined();
    expect(decode("graph", layout)).toBeUndefined();
    expect(decode("{broken", (v) => (typeof v === "object" ? v : undefined))).toBeUndefined();
  });

  it("takes a string that happens to parse as JSON as the string", () => {
    const yes = oneOf("true", "false");
    expect(decode("true", yes)).toBe("true");
  });
});
