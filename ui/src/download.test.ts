import { afterEach, describe, expect, it, vi } from "vitest";
import { codeArchiveUrl, filenameOf, formatRefusal, nodeExportUrl, saveQuery, viewExportUrl } from "./download";

describe("download urls", () => {
  it("names a node's output by format, part, run and parameters", () => {
    expect(nodeExportUrl("prices", "csv")).toBe("/api/nodes/prices/export?format=csv");
    expect(nodeExportUrl("fit", "parquet", { part: "coefs" })).toBe("/api/nodes/fit/export?format=parquet&part=coefs");
    expect(nodeExportUrl("fit", "zip", { part: null, runId: "r1" })).toBe("/api/nodes/fit/export?format=zip&run_id=r1");
    const url = new URL(nodeExportUrl("day", "xlsx", { params: { as_of: "2026-03-02" }, filename: "my day" }), "http://x");
    expect(url.searchParams.get("params")).toBe('{"as_of":"2026-03-02"}');
    expect(url.searchParams.get("filename")).toBe("my day");
    // No parameters: no params field at all.
    expect(nodeExportUrl("day", "csv", { params: {} })).toBe("/api/nodes/day/export?format=csv");
  });

  it("goes through the view's scoped route for a view", () => {
    expect(viewExportUrl("board", "fit", "json", { part: "metrics" })).toBe("/api/views/board/data/fit/export?format=json&part=metrics");
  });

  it("narrows the code archive to a node or a view", () => {
    expect(codeArchiveUrl()).toBe("/api/code/archive");
    expect(codeArchiveUrl({ node: "prices" })).toBe("/api/code/archive?node=prices");
    expect(codeArchiveUrl({ view: "board" })).toBe("/api/code/archive?view=board");
  });
});

describe("filenameOf", () => {
  it("prefers the UTF-8 name and falls back to the plain one", () => {
    expect(filenameOf(`attachment; filename="prix-_.csv"; filename*=UTF-8''prix-%C3%A9.csv`)).toBe("prix-é.csv");
    expect(filenameOf('attachment; filename="query.xlsx"')).toBe("query.xlsx");
    expect(filenameOf("attachment; filename=plain.csv")).toBe("plain.csv");
    expect(filenameOf(null)).toBe(null);
    expect(filenameOf("inline")).toBe(null);
  });
});

describe("formatRefusal", () => {
  it("refuses Excel past one sheet's rows, and nothing else", () => {
    expect(formatRefusal("xlsx", 1_048_575)).toBe(null);
    expect(formatRefusal("xlsx", 1_048_576)).toContain("CSV or Parquet");
    expect(formatRefusal("csv", 10_000_000)).toBe(null);
    expect(formatRefusal("xlsx", null)).toBe(null);
  });
});

describe("saveQuery", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts the query to the export route and reports the server's error", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ detail: "no table nope" }), { status: 400 }));
    vi.stubGlobal("fetch", fetch);
    await expect(saveQuery("select * from nope", "csv", { view: "board", params: { as_of: "x" } })).rejects.toThrow("no table nope");
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/views/board/query/export");
    expect(JSON.parse(init.body as string)).toEqual({ sql: "select * from nope", format: "csv", params: { as_of: "x" }, filename: null });
  });
});
