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

/** Whether a column's values are numbers (written at the end of their cells, in tabular figures), by its SQL type. */
export const isNumeric = (type: string) => /^(u?(tiny|small|big|huge)?int(eger|\d+)?$|float|double|decimal|numeric|real)/i.test(type);

/** How a column's values are tested by a filter of its own (the funnel in its header). */
export type FilterOp = "contains" | "not_contains" | "eq" | "ne" | "starts" | "ends" | "gt" | "ge" | "lt" | "le" | "null" | "not_null";

/** One column's filter: its values are tested against `value` (which `null` and `not_null` do not read). */
export interface ColumnFilter {
  column: string;
  op: FilterOp;
  value: string;
}

/** What a column holds, as far as filtering it goes: numbers and times are compared, anything else is matched as text. */
export type ColumnKind = "number" | "time" | "text";

export const columnKind = (type: string): ColumnKind => (isNumeric(type) ? "number" : /^(date|time)/i.test(type) ? "time" : "text");

const COMPARED: FilterOp[] = ["eq", "ne", "gt", "ge", "lt", "le", "null", "not_null"];
const MATCHED: FilterOp[] = ["contains", "not_contains", "eq", "ne", "starts", "ends", "null", "not_null"];

/** The tests a column of this kind can be filtered by, the first the one offered. */
export const filterOps = (kind: ColumnKind): FilterOp[] => (kind === "text" ? MATCHED : COMPARED);

/** Whether a test reads a value. */
export const needsValue = (op: FilterOp) => op !== "null" && op !== "not_null";

/** A test in words, as a column of this kind reads it: `price ≥ 100`, `date is after 2024-01-01`. */
export function opLabel(op: FilterOp, kind: ColumnKind): string {
  const time = kind === "time";
  switch (op) {
    case "contains":
      return "contains";
    case "not_contains":
      return "does not contain";
    case "starts":
      return "starts with";
    case "ends":
      return "ends with";
    case "eq":
      return kind === "number" ? "=" : "is";
    case "ne":
      return kind === "number" ? "≠" : "is not";
    case "gt":
      return time ? "is after" : ">";
    case "ge":
      return time ? "is on or after" : "≥";
    case "lt":
      return time ? "is before" : "<";
    case "le":
      return time ? "is on or before" : "≤";
    case "null":
      return "is missing";
    case "not_null":
      return "is not missing";
  }
}

/** A column's filter in a few words: `price ≥ 100`, `name contains "inc"`, `note is missing`. */
export function filterText(f: ColumnFilter, type: string): string {
  const kind = columnKind(type);
  const label = opLabel(f.op, kind);
  if (!needsValue(f.op)) return `${f.column} ${label}`;
  return `${f.column} ${label} ${kind === "text" ? `"${f.value}"` : f.value.trim()}`;
}

const quoted = (v: string) => `'${v.replace(/'/g, "''")}'`;
const SYMBOL: Partial<Record<FilterOp, string>> = { eq: "=", gt: ">", ge: ">=", lt: "<", le: "<=" };

/**
 * A column's filter as a SQL condition, for a column of this `type`; null
 * when it tests nothing (a test that reads a value, and none is typed).
 * What is typed is only ever a number or a quoted string. Text is matched
 * whatever its case (`contains`, `starts with`, `ends with`), `is` is the
 * value itself; a row without a value passes `is not` and `does not
 * contain`, and no other test but `is missing`.
 */
export function filterWhere(f: ColumnFilter, type: string): string | null {
  const c = ident(f.column);
  if (f.op === "null") return `${c} is null`;
  if (f.op === "not_null") return `${c} is not null`;
  const kind = columnKind(type);
  // A number or a time is as typed without the space around it; text may be about a space.
  const typed = kind === "text" ? f.value : f.value.trim();
  if (!typed) return null;
  // A number as itself, anything else as a string DuckDB reads as the column's type (a date, a time).
  const value = kind === "number" && /^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(typed) ? typed : quoted(typed);
  // What is not text already (a boolean, a list) is matched as the grid writes it.
  const left = kind === "text" && !/^(varchar|string|text|char)/i.test(type) ? `cast(${c} as varchar)` : c;
  switch (f.op) {
    case "contains":
      return `contains(lower(${left}), lower(${value}))`;
    case "not_contains":
      return `(${c} is null or not contains(lower(${left}), lower(${value})))`;
    case "starts":
      return `starts_with(lower(${left}), lower(${value}))`;
    case "ends":
      return `ends_with(lower(${left}), lower(${value}))`;
    case "ne":
      return `${left} is distinct from ${value}`;
    default:
      return `${left} ${SYMBOL[f.op]} ${value}`;
  }
}

/** The filters after `column`'s is set to `next` (null: taken off): one a column, in the order they were first set. */
export function withFilter(filters: ColumnFilter[], column: string, next: ColumnFilter | null): ColumnFilter[] {
  if (!next) return filters.filter((f) => f.column !== column);
  return filters.some((f) => f.column === column) ? filters.map((f) => (f.column === column ? next : f)) : [...filters, next];
}
