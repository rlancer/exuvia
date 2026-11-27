/**
 * Reading exuvia's Arrow answers.
 *
 * The query and data routes answer with an Arrow IPC stream when asked
 * (`Accept: application/vnd.apache.arrow.stream`). What their JSON answers
 * carry besides the rows (DuckDB column types, and for a node's output its
 * run and row count) is in the schema metadata under `exuvia`, as JSON.
 */
import { DataType, IntervalUnit, TimeUnit, tableFromIPC, type Table } from "apache-arrow";
import type { Column } from "./api";

export const ARROW_STREAM = "application/vnd.apache.arrow.stream";

export interface ArrowMeta {
  columns?: Column[];
  kind?: "table";
  total?: number | null;
  run_id?: string;
  run_params?: Record<string, unknown>;
  finished_at?: number | null;
}

export function isArrow(res: Response): boolean {
  return (res.headers.get("Content-Type") ?? "").toLowerCase().startsWith(ARROW_STREAM);
}

/** The answer's batches, decoded as they arrive rather than once the whole body is in. */
export async function readArrow(res: Response): Promise<Table> {
  return tableFromIPC(res);
}

export function arrowMeta(table: Table): ArrowMeta {
  const raw = table.schema.metadata.get("exuvia");
  if (!raw) return {};
  try {
    return JSON.parse(raw) as ArrowMeta;
  } catch {
    return {};
  }
}

type Convert = (v: unknown) => unknown;

const pad = (n: number, width = 2) => String(n).padStart(width, "0");

/** An unscaled integer, as its decimal digits, with the point `scale` places from the right. */
function scaled(digits: string, scale: number): number | string {
  if (scale <= 0) {
    const n = Number(digits);
    return Number.isSafeInteger(n) ? n : digits;
  }
  const negative = digits.startsWith("-");
  const body = (negative ? digits.slice(1) : digits).padStart(scale + 1, "0");
  return Number(`${negative ? "-" : ""}${body.slice(0, -scale)}.${body.slice(-scale)}`);
}

/** Milliseconds since the epoch as ISO text: to the microsecond when there are any, `Z` when the column has a zone. */
function isoTimestamp(ms: number, zoned: boolean): string {
  const whole = Math.floor(ms);
  const iso = new Date(whole).toISOString(); // 2026-11-06T14:30:05.123Z
  const micros = Math.round((ms - whole) * 1000);
  let text = iso.slice(0, 19);
  if (micros) text += `${iso.slice(19, 23)}${pad(micros, 3)}`;
  else if (iso.slice(20, 23) !== "000") text += iso.slice(19, 23);
  return zoned ? `${text}Z` : text;
}

const PER_SECOND: Record<number, number> = {
  [TimeUnit.SECOND]: 1,
  [TimeUnit.MILLISECOND]: 1e3,
  [TimeUnit.MICROSECOND]: 1e6,
  [TimeUnit.NANOSECOND]: 1e9,
};

function timeOfDay(v: number | bigint, unit: TimeUnit): string {
  const per = PER_SECOND[unit] ?? 1;
  const total = Number(v);
  const secs = Math.floor(total / per);
  const frac = total - secs * per;
  const text = `${pad(Math.floor(secs / 3600))}:${pad(Math.floor(secs / 60) % 60)}:${pad(secs % 60)}`;
  return frac ? `${text}.${pad(frac, String(per).length - 1).replace(/0+$/, "")}` : text;
}

/** A months / days / nanoseconds interval (what DuckDB's INTERVAL arrives as), the way DuckDB prints one. */
function monthDayNano(v: Int32Array): string {
  const [months, days] = v;
  const nanos = BigInt.asIntN(64, (BigInt(v[3]) << 32n) | BigInt(v[2] >>> 0));
  const parts: string[] = [];
  const count = (n: number, unit: string) => n && parts.push(`${n} ${unit}${Math.abs(n) === 1 ? "" : "s"}`);
  count(Math.trunc(months / 12), "year");
  count(months % 12, "month");
  count(days, "day");
  if (nanos || !parts.length) {
    const text = timeOfDay(nanos < 0n ? -nanos : nanos, TimeUnit.NANOSECOND);
    parts.push(nanos < 0n ? `-${text}` : text);
  }
  return parts.join(" ");
}

/** How to turn one Arrow value of `type` into a plain value a table cell or a chart can take. */
function converter(type: DataType): Convert {
  if (DataType.isDictionary(type)) return converter(type.dictionary);
  if (DataType.isInt(type)) {
    return (v) => {
      if (typeof v !== "bigint") return v;
      const n = Number(v);
      return Number.isSafeInteger(n) ? n : v.toString();
    };
  }
  if (DataType.isDecimal(type)) return (v) => scaled(String(v), type.scale);
  if (DataType.isDate(type)) return (v) => new Date(v as number).toISOString().slice(0, 10);
  if (DataType.isTimestamp(type)) {
    const zoned = !!type.timezone;
    return (v) => isoTimestamp(v as number, zoned);
  }
  if (DataType.isTime(type)) return (v) => timeOfDay(v as number | bigint, type.unit);
  if (DataType.isInterval(type) && type.unit === IntervalUnit.MONTH_DAY_NANO) {
    return (v) => monthDayNano(v as Int32Array);
  }
  if (DataType.isBinary(type) || DataType.isLargeBinary(type) || DataType.isFixedSizeBinary(type)) {
    return (v) => `\\x${Array.from(v as Uint8Array, (b) => b.toString(16).padStart(2, "0")).join("")}`;
  }
  if (DataType.isList(type) || DataType.isFixedSizeList(type)) {
    const item = converter(type.children[0].type);
    return (v) => Array.from(v as Iterable<unknown>, (x) => (x == null ? null : item(x)));
  }
  if (DataType.isStruct(type)) {
    const fields = type.children.map((f) => [f.name, converter(f.type)] as const);
    return (v) => {
      const row = v as Record<string, unknown>;
      return Object.fromEntries(fields.map(([name, conv]) => [name, row[name] == null ? null : conv(row[name])]));
    };
  }
  if (DataType.isMap(type)) {
    const [key, value] = type.children[0].type.children.map((f) => converter(f.type));
    return (v) =>
      Object.fromEntries(
        Array.from(v as Iterable<[unknown, unknown]>, ([k, x]) => [String(key(k)), x == null ? null : value(x)]),
      );
  }
  if (DataType.isFloat(type) || DataType.isBool(type) || DataType.isUtf8(type) || DataType.isLargeUtf8(type)) {
    return (v) => v;
  }
  return (v) => (typeof v === "object" || typeof v === "bigint" ? String(v) : v);
}

/** The table as the rows-of-cells shape the JSON routes answer with. */
export function toGrid(table: Table): { columns: Column[]; rows: unknown[][] } {
  const fields = table.schema.fields;
  const named = arrowMeta(table).columns;
  const columns =
    named && named.length === fields.length ? named : fields.map((f) => ({ name: f.name, type: String(f.type) }));
  const vectors = fields.map((_, i) => table.getChildAt(i)!);
  const convert = fields.map((f) => converter(f.type));
  const rows: unknown[][] = new Array(table.numRows);
  for (let r = 0; r < table.numRows; r++) {
    const row = new Array(fields.length);
    for (let c = 0; c < fields.length; c++) {
      const v = vectors[c].get(r);
      row[c] = v == null ? null : convert[c](v);
    }
    rows[r] = row;
  }
  return { columns, rows };
}
