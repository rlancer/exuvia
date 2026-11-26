import { describe, expect, it } from "vitest";
import { columnChars, pagesFor, pagesToDrop, rowWindow } from "./rowWindow";

describe("rowWindow", () => {
  const at = (scrollTop: number, total = 1000) => rowWindow({ scrollTop, viewport: 300, rowHeight: 30, total, overscan: 5 });

  it("draws the rows in view and a few on each side", () => {
    expect(at(0)).toEqual({ start: 0, end: 15, padTop: 0, padBottom: 985 * 30 });
    expect(at(3000)).toEqual({ start: 95, end: 115, padTop: 95 * 30, padBottom: 885 * 30 });
  });

  it("stands for every row it does not draw", () => {
    for (const top of [0, 17, 4000, 29_700, 99_999]) {
      const w = at(top);
      expect(w.padTop + (w.end - w.start) * 30 + w.padBottom).toBe(1000 * 30);
    }
  });

  it("ends at the last row", () => {
    expect(at(1000 * 30 - 300)).toMatchObject({ start: 985, end: 1000, padBottom: 0 });
    expect(at(1e9)).toMatchObject({ end: 1000, padBottom: 0 });
  });

  it("draws nothing of no rows, and all of a few", () => {
    expect(at(0, 0)).toEqual({ start: 0, end: 0, padTop: 0, padBottom: 0 });
    expect(at(0, 3)).toEqual({ start: 0, end: 3, padTop: 0, padBottom: 0 });
  });

  describe("past the tallest a browser lays out", () => {
    const total = 10_000_000;
    const maxHeight = 1_000_000;
    const big = (scrollTop: number) => rowWindow({ scrollTop, viewport: 300, rowHeight: 30, total, overscan: 5, maxHeight });

    it("is never taller than that", () => {
      for (const top of [0, 1234, 500_000, maxHeight - 300]) {
        const w = big(top);
        expect(w.padTop + (w.end - w.start) * 30 + w.padBottom).toBe(maxHeight);
      }
    });

    it("starts at the first row and reaches the last", () => {
      expect(big(0)).toMatchObject({ start: 0, padTop: 0 });
      expect(big(maxHeight - 300)).toMatchObject({ end: total, padBottom: 0 });
    });

    it("puts the first row in view where the area is scrolled to", () => {
      const w = big(500_000);
      // Half way down: the row half way through, five rows of overscan over it.
      expect(w.start + 5).toBe(Math.round((500_000 / (maxHeight - 300)) * (total - 10)));
      expect(w.padTop).toBe(500_000 - 5 * 30);
    });
  });
});

describe("pagesFor", () => {
  it("names the pages that hold a range of rows", () => {
    expect(pagesFor(0, 30, 500)).toEqual([0]);
    expect(pagesFor(490, 510, 500)).toEqual([0, 1]);
    expect(pagesFor(500, 1000, 500)).toEqual([1]);
    expect(pagesFor(5, 5, 500)).toEqual([]);
  });
});

describe("pagesToDrop", () => {
  it("drops nothing while few are held", () => {
    expect(pagesToDrop([0, 1, 2], 1, 1, 3)).toEqual([]);
  });

  it("drops the pages furthest from the ones in view", () => {
    expect(pagesToDrop([0, 1, 7, 8, 9, 40], 8, 9, 3).sort((a, b) => a - b)).toEqual([0, 1, 40]);
  });
});

describe("columnChars", () => {
  it("only ever widens a column, up to the cap", () => {
    const first = columnChars([["ab", 1], ["abcd", 22]], String, [], 10);
    expect(first).toEqual([4, 2]);
    expect(columnChars([["a", 333], ["x".repeat(50), 1]], String, first, 10)).toEqual([10, 3]);
  });
});
