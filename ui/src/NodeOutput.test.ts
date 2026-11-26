import { describe, expect, it } from "vitest";
import type { GraphNode } from "./api";
import { chartBrush, isNumeric, outputPart, pct, rowsSource, selectedRows } from "./NodeOutput";

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

  it("opens a charted node on the output its chart draws: its first table", () => {
    const fit = {
      ...node("multi"),
      chart: { mark: "bar" },
      schema: [
        { name: "metrics", type: "json" },
        { name: "coefs", type: "table" },
        { name: "preds", type: "table" },
      ],
    } as GraphNode;
    expect(outputPart(fit, null).shown).toBe("coefs");
    expect(outputPart(fit, "metrics").shown).toBe("metrics");
    expect(outputPart({ ...fit, chart: null }, null).shown).toBe("metrics");
  });
});

describe("isNumeric", () => {
  it("knows DuckDB's and Arrow's number types", () => {
    for (const t of ["INTEGER", "BIGINT", "UTINYINT", "HUGEINT", "DOUBLE", "FLOAT", "DECIMAL(18,3)", "Int64", "Uint8", "Float64"]) expect(isNumeric(t), t).toBe(true);
  });
  it("does not take a type that only starts like one for a number", () => {
    for (const t of ["INTERVAL", "VARCHAR", "TIMESTAMP", "DATE", "BOOLEAN", "INTEGER[]", "POINT_2D"]) expect(isNumeric(t), t).toBe(false);
  });
});

describe("rowsSource", () => {
  it("serves rows that are already here, and how wide their columns are", () => {
    const src = rowsSource([{ name: "ticker", type: "VARCHAR" }, { name: "n", type: "BIGINT" }], [["AAPL", 1], ["a-much-longer-name", 1234567]]);
    expect(src.total).toBe(2);
    expect(src.row(1)).toEqual(["a-much-longer-name", 1234567]);
    expect(src.row(2)).toBeUndefined();
    // The header's own text counts, and a number as the grid writes it ("1,234,567").
    expect(src.chars).toEqual([18, 9]);
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

describe("chartBrush", () => {
  const enc = (x: object, y: object = { field: "v", type: "quantitative" }) => ({ x, y });
  it("brushes a continuous x, and y too for points", () => {
    expect(chartBrush({ mark: "line", encoding: enc({ field: "d", type: "temporal" }) })).toMatchObject({ kind: "interval", fields: ["d"] });
    expect(chartBrush({ mark: { type: "point" }, encoding: enc({ field: "a", type: "quantitative" }) })).toMatchObject({ kind: "interval", fields: ["a", "v"] });
  });
  it("picks a category by clicking", () => {
    expect(chartBrush({ mark: "bar", encoding: enc({ field: "t", type: "nominal" }) })).toMatchObject({ kind: "point", fields: ["t"] });
  });
  it("leaves alone what a mark is not a row of, or what has params of its own", () => {
    expect(chartBrush({ mark: "bar", encoding: enc({ field: "d", type: "temporal", timeUnit: "month" }) })).toBeNull();
    expect(chartBrush({ mark: "bar", encoding: enc({ field: "t", type: "nominal", aggregate: "count" }) })).toBeNull();
    expect(chartBrush({ layer: [], encoding: enc({ field: "d", type: "temporal" }) })).toBeNull();
    expect(chartBrush({ mark: "line", params: [], encoding: enc({ field: "d", type: "temporal" }) })).toBeNull();
    expect(chartBrush(null)).toBeNull();
  });
});

describe("selectedRows", () => {
  const columns = [
    { name: "d", type: "TIMESTAMP" },
    { name: "t", type: "VARCHAR" },
    { name: "v", type: "DOUBLE" },
  ];
  const rows = [
    ["2024-01-01T00:00:00", "A", 1],
    ["2024-02-01T00:00:00", "B", 5],
    ["2024-03-01T00:00:00", "A", 9],
  ];
  it("keeps the rows inside an interval, times included", () => {
    const brush = { kind: "interval" as const, fields: ["d", "v"] };
    const lo = new Date("2024-01-15T00:00:00"), hi = new Date("2024-03-15T00:00:00");
    expect(selectedRows(brush, { d: [hi, lo], v: [0, 6] }, columns, rows)).toEqual([rows[1]]);
  });
  it("keeps the rows of the picked categories", () => {
    expect(selectedRows({ kind: "point", fields: ["t"] }, { t: ["A"] }, columns, rows)).toEqual([rows[0], rows[2]]);
  });
  it("is null for an empty selection", () => {
    expect(selectedRows({ kind: "interval", fields: ["v"] }, {}, columns, rows)).toBeNull();
    expect(selectedRows({ kind: "interval", fields: ["v"] }, null, columns, rows)).toBeNull();
  });
});
