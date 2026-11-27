import { describe, expect, it } from "vitest";
import { barLayout, barWidth } from "./barLayout";

const editor = { states: 3, buttons: true, toggle: true };

describe("barLayout", () => {
  it("shows everything where there is room", () => {
    expect(barLayout(1580, editor)).toEqual({ toggleLabels: true, buttonLabels: true, counts: true });
  });

  it("gives up the switch's words first: a 1480px window with the side panel open", () => {
    expect(barLayout(1140, editor)).toEqual({ toggleLabels: false, buttonLabels: true, counts: true });
  });

  it("then the buttons' words, then the counts", () => {
    expect(barLayout(900, editor)).toEqual({ toggleLabels: false, buttonLabels: false, counts: true });
    expect(barLayout(640, editor)).toEqual({ toggleLabels: false, buttonLabels: false, counts: false });
    expect(barLayout(300, editor)).toEqual({ toggleLabels: false, buttonLabels: false, counts: false });
  });

  it("never picks a layout wider than the bar when one fits", () => {
    for (let w = 500; w <= 2000; w += 7) {
      for (const has of [editor, { states: 6, buttons: true, toggle: true }, { states: 2, buttons: false, toggle: true }, { states: 1, buttons: true, toggle: false }]) {
        const l = barLayout(w, has);
        const smallest = barWidth({ toggleLabels: false, buttonLabels: false, counts: false }, has);
        if (w >= smallest) expect(barWidth(l, has), `${w} ${JSON.stringify(has)}`).toBeLessThanOrEqual(w);
      }
    }
  });

  it("keeps more words for a viewer, who has no buttons", () => {
    expect(barLayout(900, { states: 3, buttons: false, toggle: true })).toEqual({ toggleLabels: true, buttonLabels: true, counts: true });
  });

  it("counts more states as wider", () => {
    expect(barWidth({ toggleLabels: true, buttonLabels: true, counts: true }, { ...editor, states: 6 })).toBeGreaterThan(
      barWidth({ toggleLabels: true, buttonLabels: true, counts: true }, editor),
    );
  });
});
