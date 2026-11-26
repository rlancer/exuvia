import { describe, expect, it } from "vitest";
import type { ProjectParam } from "../api";
import { mergeParams, paramsState } from "./params";

describe("mergeParams", () => {
  it("lays the change over the overrides", () => {
    expect(mergeParams({ day: "2026-01-02", n: 3 }, { n: 5, flag: true })).toEqual({ day: "2026-01-02", n: 5, flag: true });
  });

  it("drops a parameter set to null or empty: back to its default", () => {
    expect(mergeParams({ day: "2026-01-02", name: "x", n: 3 }, { day: null, name: "" })).toEqual({ n: 3 });
  });

  it("keeps false and 0, which are values", () => {
    expect(mergeParams(null, { flag: false, n: 0 })).toEqual({ flag: false, n: 0 });
  });

  it("does not change the overrides it was given", () => {
    const before = { n: 3 };
    mergeParams(before, { n: null });
    expect(before).toEqual({ n: 3 });
  });
});

describe("paramsState", () => {
  it("is the values by name, with the declarations", () => {
    const specs = [
      { name: "day", type: "date", default: null, value: "2026-01-02" },
      { name: "n", type: "number", default: 1, value: 3 },
    ] as ProjectParam[];
    expect(paramsState(specs)).toEqual({ params: { day: "2026-01-02", n: 3 }, specs });
  });
});
