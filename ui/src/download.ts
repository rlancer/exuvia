/**
 * Downloads: a node's output or a SQL result as CSV, Excel, Parquet or JSON,
 * and code as a zip (exuvia/exports.py serves them).
 *
 * A node's output and the code archive are GETs: the browser saves the answer
 * straight to disk through a link (the session cookie goes with it), so a big
 * table never passes through the page. A SQL result is a POST: it is fetched
 * and handed to the browser as a file.
 */
import { apiUrl } from "./api";
import { noteAnswer } from "./session";

export type TableFormat = "csv" | "xlsx" | "parquet" | "json";

export const TABLE_FORMATS: { format: TableFormat; label: string; hint: string }[] = [
  { format: "csv", label: "CSV", hint: ".csv: opens anywhere" },
  { format: "xlsx", label: "Excel", hint: ".xlsx: typed cells, one sheet" },
  { format: "parquet", label: "Parquet", hint: ".parquet: typed columns, for pandas, DuckDB, Spark" },
  { format: "json", label: "JSON", hint: ".json: an array of rows" },
];

/** One Excel sheet holds this many rows below its header. */
export const EXCEL_MAX_ROWS = 1_048_575;

/** Why a format cannot hold a table of `rows` rows, or null. */
export function formatRefusal(format: TableFormat, rows: number | null | undefined): string | null {
  if (format === "xlsx" && rows != null && rows > EXCEL_MAX_ROWS)
    return `Too many rows for Excel (one sheet holds ${EXCEL_MAX_ROWS.toLocaleString()}): use CSV or Parquet.`;
  return null;
}

type Params = Record<string, unknown>;

function query(fields: Record<string, string | null | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(fields)) if (v != null && v !== "") q.set(k, v);
  const s = q.toString();
  return s ? `?${s}` : "";
}

const paramsField = (params?: Params) => (params && Object.keys(params).length ? JSON.stringify(params) : null);

/** A node's output as a file: `zip` is every output of a node with several; `part` picks one of them. */
export function nodeExportUrl(
  node: string,
  format: TableFormat | "zip",
  o: { part?: string | null; runId?: string | null; params?: Params; filename?: string } = {},
): string {
  return apiUrl(
    `/api/nodes/${encodeURIComponent(node)}/export` +
      query({ format, part: o.part, run_id: o.runId, params: paramsField(o.params), filename: o.filename }),
  );
}

/** The same through a view's scoped route (403 unless the view reads `node`). */
export function viewExportUrl(
  view: string,
  node: string,
  format: TableFormat,
  o: { part?: string | null; params?: Params; filename?: string } = {},
): string {
  return apiUrl(
    `/api/views/${encodeURIComponent(view)}/data/${encodeURIComponent(node)}/export` +
      query({ format, part: o.part, params: paramsField(o.params), filename: o.filename }),
  );
}

/** The project's code as a zip, or one node's (with the helpers it imports), or one view's. */
export function codeArchiveUrl(o: { node?: string; view?: string } = {}): string {
  return apiUrl(`/api/code/archive${query({ node: o.node, view: o.view })}`);
}

function click(href: string, filename = ""): void {
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Have the browser save `href` (a data: or blob: URL made here) as `filename`. */
export function saveAs(href: string, filename: string): void {
  click(href, filename);
}

/** Have the browser save what a GET of `url` answers (the server names the file). */
export function saveUrl(url: string): void {
  click(url);
}

/** The file name a `Content-Disposition` header gives, or null. */
export function filenameOf(disposition: string | null): string | null {
  if (!disposition) return null;
  const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim());
    } catch {
      /* fall back to the plain name */
    }
  }
  const plain = /filename\s*=\s*"([^"]*)"|filename\s*=\s*([^;]+)/i.exec(disposition);
  return plain ? (plain[1] ?? plain[2]).trim() : null;
}

/** Save the whole result of a SQL query; `view` runs it with only that view's reads. */
export async function saveQuery(
  sql: string,
  format: TableFormat,
  o: { view?: string; params?: Params; filename?: string } = {},
): Promise<void> {
  const path = o.view ? `/api/views/${encodeURIComponent(o.view)}/query/export` : "/api/query/export";
  const res = await fetch(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sql, format, params: o.params && Object.keys(o.params).length ? o.params : null, filename: o.filename ?? null }),
  });
  noteAnswer(res.status);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      msg = (await res.json()).detail ?? msg;
    } catch {
      /* not json */
    }
    throw new Error(msg);
  }
  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  click(href, filenameOf(res.headers.get("content-disposition")) ?? `${o.filename ?? "query"}.${format}`);
  setTimeout(() => URL.revokeObjectURL(href), 60_000);
}
