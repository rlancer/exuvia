/**
 * The SQL behind the Data tab's table when it shows anything but the output
 * as stored: rows filtered by a condition, picked on the chart, sorted by a
 * column, or a query of the user's own. Pure, so it is tested on its own
 * (tableQuery.test.ts).
 */

/** A column the table is sorted by, and which way. */
export interface Sort {
  column: string;
  desc: boolean;
}

/** What the table shows of an output, besides the output itself. */
export interface TableAsk {
  /** A query of the user's own: then `where` is not applied (it is theirs to write). */
  sql: string | null;
  /** Conditions on the output's rows, all of which hold: the filter typed, what is picked on the chart. */
  where: string[];
  sort: Sort | null;
}

/** A name as SQL reads it whatever is in it. */
export const ident = (name: string) => `"${name.replace(/"/g, '""')}"`;

/** A node's output (or one of its outputs, `part`) as a table in SQL. */
export const tableOf = (node: string, part?: string | null) => (part ? `${ident(node)}.${ident(part)}` : ident(node));

/** A query as typed, ready to stand inside another: no `;` at its end, on lines of its own (a `--` comment may end it). */
const inner = (sql: string) => `(\n${sql.trim().replace(/;+\s*$/, "")}\n)`;

/** Whether anything is asked of the output: nothing asked, and the table reads it as stored. */
export function asks(ask: TableAsk): boolean {
  return !!ask.sql?.trim() || ask.where.some((w) => w.trim()) || !!ask.sort;
}

/** The rows asked for, unsorted: the user's query, or the output where every condition holds. */
export function baseQuery(table: string, ask: TableAsk): string {
  if (ask.sql?.trim()) return ask.sql.trim().replace(/;+\s*$/, "");
  const where = ask.where.map((w) => w.trim()).filter(Boolean);
  return `select * from ${table}${where.length ? ` where ${where.map((w) => `(${w})`).join(" and ")}` : ""}`;
}

/** How many rows `baseQuery` has. */
export function countQuery(table: string, ask: TableAsk): string {
  return `select count(*) as n from ${inner(baseQuery(table, ask))} as q`;
}

/**
 * The query whose pages the table reads. Unsorted, it is `baseQuery` as it
 * is. Sorted, the rows are ordered by the column and then by every other
 * column (`columns` are the result's): each page runs the query again, and
 * rows that tie on the column would come in another order each time, so a
 * row would show twice or not at all where two pages meet. Nulls go last
 * either way.
 */
export function pageQuery(table: string, ask: TableAsk, columns: string[]): string {
  const base = baseQuery(table, ask);
  if (!ask.sort) return base;
  const { column, desc } = ask.sort;
  const rest = columns.filter((c) => c !== column).map(ident);
  return `select * from ${inner(base)} as q order by ${[`${ident(column)}${desc ? " desc" : ""} nulls last`, ...rest].join(", ")}`;
}

/** The sort after a click on `column`'s header: ascending, then descending, then none. */
export function nextSort(sort: Sort | null, column: string): Sort | null {
  if (sort?.column !== column) return { column, desc: false };
  return sort.desc ? null : { column, desc: true };
}
