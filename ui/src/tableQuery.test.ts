import { describe, expect, it } from "vitest";
import { asks, baseQuery, columnKind, countQuery, filterOps, filterText, filterWhere, nextSort, pageQuery, tableOf, withFilter, type ColumnFilter, type TableAsk } from "./tableQuery";

const ask = (a: Partial<TableAsk> = {}): TableAsk => ({ sql: null, where: [], sort: null, ...a });

describe("tableOf", () => {
  it("quotes a node, and one of its outputs", () => {
    expect(tableOf("prices")).toBe('"prices"');
    expect(tableOf("fit", "coefs")).toBe('"fit"."coefs"');
    expect(tableOf('we"ird')).toBe('"we""ird"');
  });
});

describe("asks", () => {
  it("is nothing until a filter, a selection, a sort or a query is", () => {
    expect(asks(ask())).toBe(false);
    expect(asks(ask({ where: ["", "  "], sql: "  " }))).toBe(false);
    expect(asks(ask({ where: ["x > 1"] }))).toBe(true);
    expect(asks(ask({ sort: { column: "x", desc: false } }))).toBe(true);
    expect(asks(ask({ sql: "select 1" }))).toBe(true);
  });
});

describe("baseQuery", () => {
  it("is the output where every condition holds", () => {
    expect(baseQuery('"t"', ask())).toBe('select * from "t"');
    expect(baseQuery('"t"', ask({ where: ["a = 1 or b = 2", "", "c > 3"] }))).toBe('select * from "t" where (a = 1 or b = 2) and (c > 3)');
  });

  it("is the user's query as written, without its semicolon, when there is one", () => {
    expect(baseQuery('"t"', ask({ sql: " select k, count(*) from t group by 1; ", where: ["ignored"] }))).toBe("select k, count(*) from t group by 1");
  });
});

describe("countQuery", () => {
  it("counts the rows asked for, whatever the query ends with", () => {
    expect(countQuery('"t"', ask({ sql: "select * from t -- every row" }))).toBe("select count(*) as n from (\nselect * from t -- every row\n) as q");
  });
});

describe("pageQuery", () => {
  it("is the rows asked for as they are when nothing sorts them", () => {
    expect(pageQuery('"t"', ask({ where: ["a > 1"] }), ["a", "b"])).toBe('select * from "t" where (a > 1)');
  });

  it("orders by the column, then by every other so that pages agree", () => {
    expect(pageQuery('"t"', ask({ sort: { column: "b", desc: true } }), ["a", "b", "c d"])).toBe(
      'select * from (\nselect * from "t"\n) as q order by "b" desc nulls last, "a", "c d"',
    );
    expect(pageQuery('"t"', ask({ sort: { column: "a", desc: false } }), ["a"])).toBe('select * from (\nselect * from "t"\n) as q order by "a" nulls last');
  });
});

describe("nextSort", () => {
  it("goes up, then down, then back to the order stored", () => {
    const up = nextSort(null, "a");
    expect(up).toEqual({ column: "a", desc: false });
    const down = nextSort(up, "a");
    expect(down).toEqual({ column: "a", desc: true });
    expect(nextSort(down, "a")).toBeNull();
    expect(nextSort(down, "b")).toEqual({ column: "b", desc: false });
  });
});

const on = (column: string, op: ColumnFilter["op"], value = ""): ColumnFilter => ({ column, op, value });

describe("columnKind", () => {
  it("compares numbers and times, and matches anything else as text", () => {
    for (const t of ["BIGINT", "DOUBLE", "DECIMAL(18,3)"]) expect(columnKind(t), t).toBe("number");
    for (const t of ["DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIME"]) expect(columnKind(t), t).toBe("time");
    for (const t of ["VARCHAR", "BOOLEAN", "INTEGER[]", "STRUCT(a INTEGER)"]) expect(columnKind(t), t).toBe("text");
    expect(filterOps("text")[0]).toBe("contains");
    expect(filterOps("number")).not.toContain("contains");
  });
});

describe("filterWhere", () => {
  it("compares a number as a number, and what is not one as a string for DuckDB to refuse", () => {
    expect(filterWhere(on("p", "ge", " 100 "), "DOUBLE")).toBe('"p" >= 100');
    expect(filterWhere(on("p", "eq", "-1.5e3"), "BIGINT")).toBe('"p" = -1.5e3');
    expect(filterWhere(on("p", "lt", "1; drop table t"), "BIGINT")).toBe("\"p\" < '1; drop table t'");
  });

  it("compares a time with what is typed, as a string", () => {
    expect(filterWhere(on("d", "gt", "2024-01-31"), "DATE")).toBe("\"d\" > '2024-01-31'");
  });

  it("matches text whatever its case, quoting what is typed", () => {
    expect(filterWhere(on("we\"ird", "contains", "O'Neil"), "VARCHAR")).toBe("contains(lower(\"we\"\"ird\"), lower('O''Neil'))");
    expect(filterWhere(on("n", "starts", "a"), "VARCHAR")).toBe("starts_with(lower(\"n\"), lower('a'))");
    expect(filterWhere(on("n", "ends", "a"), "VARCHAR")).toBe("ends_with(lower(\"n\"), lower('a'))");
    expect(filterWhere(on("n", "eq", "A b"), "VARCHAR")).toBe("\"n\" = 'A b'");
  });

  it("reads what is not text as the grid writes it", () => {
    expect(filterWhere(on("ok", "eq", "true"), "BOOLEAN")).toBe("cast(\"ok\" as varchar) = 'true'");
  });

  it("lets a row without a value pass `is not` and `does not contain`", () => {
    expect(filterWhere(on("n", "ne", "a"), "VARCHAR")).toBe("\"n\" is distinct from 'a'");
    expect(filterWhere(on("n", "not_contains", "a"), "VARCHAR")).toBe("(\"n\" is null or not contains(lower(\"n\"), lower('a')))");
  });

  it("tests for a missing value without reading one, and nothing without a value to read", () => {
    expect(filterWhere(on("n", "null", "ignored"), "VARCHAR")).toBe('"n" is null');
    expect(filterWhere(on("n", "not_null"), "DOUBLE")).toBe('"n" is not null');
    expect(filterWhere(on("n", "contains", ""), "VARCHAR")).toBeNull();
    expect(filterWhere(on("p", "gt", "  "), "DOUBLE")).toBeNull();
  });
});

describe("filterText", () => {
  it("says a filter in a few words, as its column reads it", () => {
    expect(filterText(on("price", "ge", "100"), "DOUBLE")).toBe("price ≥ 100");
    expect(filterText(on("date", "lt", "2024-01-31"), "DATE")).toBe("date is before 2024-01-31");
    expect(filterText(on("name", "contains", "inc"), "VARCHAR")).toBe('name contains "inc"');
    expect(filterText(on("note", "null"), "VARCHAR")).toBe("note is missing");
  });
});

describe("withFilter", () => {
  it("keeps one filter a column: set, replaced in place, taken off", () => {
    const a = withFilter([], "a", on("a", "gt", "1"));
    const ab = withFilter(a, "b", on("b", "null"));
    expect(ab.map((f) => f.column)).toEqual(["a", "b"]);
    const again = withFilter(ab, "a", on("a", "lt", "9"));
    expect(again).toEqual([on("a", "lt", "9"), on("b", "null")]);
    expect(withFilter(again, "a", null)).toEqual([on("b", "null")]);
  });
});
