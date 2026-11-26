import { describe, expect, it } from "vitest";
import { asks, baseQuery, countQuery, nextSort, pageQuery, tableOf, type TableAsk } from "./tableQuery";

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
