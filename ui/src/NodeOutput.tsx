import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState, type ReactNode, type Ref } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, fontWeightVars, spacingVars, textSizeVars, typeScaleVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { HStack } from "@astryxdesign/core/HStack";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { api, type Column, type ColumnStats, type GraphNode, type Preview, type TableStats } from "./api";
import { columnChars, MAX_ROWS_HEIGHT, PAGE_ROWS, pagesFor, pagesToDrop, rowWindow } from "./rowWindow";
import { fmtBytes, fmtCell, fmtNum } from "./util";

/*
 * What a node made, shown wherever it is shown: the inspector's Data tab (its
 * chart over every row of it), its query box, and a notebook cell's preview.
 */

/**
 * A node's output, or one of its outputs (`part`), its first `limit` rows:
 * read again when the node makes a new one. `preview` is null until it comes
 * (it keeps the last one while the next is on its way); `failed` says the
 * read failed. Nothing is read while `enabled` is false.
 */
export function useNodePreview(node: GraphNode, limit: number, part: string | null | undefined, enabled = true): { preview: Preview | null; failed: boolean } {
  const [state, setState] = useState<{ preview: Preview | null; failed: boolean }>({ preview: null, failed: false });
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    api.data(node.name, limit, part ?? undefined).then(
      (preview) => live && setState({ preview, failed: false }),
      () => live && setState((s) => ({ ...s, failed: true })),
    );
    return () => {
      live = false;
    };
  }, [node.name, node.artifact_id, limit, part, enabled]);
  return state;
}

/**
 * A table output's (or one output's, `part`) overview from its Parquet
 * footers (GET /nodes/{name}/stats: no row is read, so it is as quick for
 * ten million rows as for ten), read again when the node makes a new one.
 * Null until it comes, and when the output has none (it is not a table).
 */
export function useTableStats(node: GraphNode, part: string | null | undefined, enabled = true): TableStats | null {
  // Held with the output it is of: another output's (a node with several) never shows as this one's.
  const of = `${node.name}/${part ?? ""}`;
  const [got, setGot] = useState<{ of: string; stats: TableStats | null }>({ of, stats: null });
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    api.stats(node.name, part ?? undefined).then(
      (stats) => live && setGot({ of, stats }),
      () => live && setGot({ of, stats: null }),
    );
    return () => {
      live = false;
    };
  }, [node.name, node.artifact_id, part, enabled, of]);
  return enabled && got.of === of ? got.stats : null;
}

/** A share as a percentage, never rounded to 0% or 100% when it is neither. */
export function pct(part: number, whole: number): string {
  if (whole <= 0) return "";
  const f = part / whole;
  if (f === 0 || f === 1) return `${f * 100}%`;
  if (f < 0.01) return "<1%";
  if (f > 0.99) return ">99%";
  return `${Math.round(f * 100)}%`;
}

/** A share in brackets after a count, and nothing when there is no whole to be a share of. */
const share = (part: number, whole: number, more = "") => {
  const p = pct(part, whole);
  return p ? ` (${p}${more})` : "";
};

/**
 * The whole output in one line, as the footers have it and in Data Wrangler's
 * words: its shape, its missing values and the columns they are in, its size
 * on disk and how it is compressed (the rest in a tooltip). With `shown`,
 * it says so when the rows on screen are fewer than the output's; without
 * `storage`, it leaves out how the output is stored (size, compression).
 */
export function DatasetSummary({ stats: s, shown, storage = true }: { stats: TableStats; shown?: number; storage?: boolean }) {
  const known = s.columns.filter((c) => c.nulls != null);
  const missing = known.reduce((n, c) => n + (c.nulls ?? 0), 0);
  const withMissing = known.filter((c) => (c.nulls ?? 0) > 0).length;
  const cells = known.length * s.rows;
  const line = (
    <Text type="supporting" color="secondary" maxLines={2} hasTabularNumbers>
      {shown != null && shown < s.rows ? `First ${fmtNum(shown)} shown · ` : ""}
      {`Data shape ${fmtNum(s.rows)} rows × ${fmtNum(s.columns.length)} columns`}
      {` · Missing values ${fmtNum(missing)}${share(missing, cells, withMissing ? `, in ${withMissing} column${withMissing === 1 ? "" : "s"}` : "")}`}
      {/* No row group, no codec to name: an empty output is not called uncompressed. */}
      {storage && ` · ${fmtBytes(s.bytes)}${s.row_groups ? ` ${s.compression.map((c) => c.toLowerCase()).join(", ") || "uncompressed"}` : ""}`}
    </Text>
  );
  if (!storage) return line;
  // The size on disk is the whole file, footer and all; the columns' data alone is smaller.
  const detail = [
    `column data ${fmtBytes(s.uncompressed_bytes)} before compression`,
    `${fmtNum(s.row_groups)} row group${s.row_groups === 1 ? "" : "s"}${s.files > 1 ? ` in ${fmtNum(s.files)} files` : ""}`,
    ...s.created_by.map((w) => `written by ${w}`),
  ];
  return <Tooltip content={detail.join(" · ")}>{line}</Tooltip>;
}

/**
 * The output a node's chart draws: its first table when it has several
 * outputs, and none of them (null: the output itself) when it has one.
 */
export function chartedPart(node: GraphNode): string | null {
  return node.output_kind === "multi" ? ((node.schema ?? []).find((c) => c.type === "table")?.name ?? null) : null;
}

/**
 * Which output of a node is shown: none for a node with one output; for one
 * with several, the one picked while it still has it, else the one its
 * chart draws (so the chart shows without a pick), else its first.
 */
export function outputPart(node: GraphNode, picked: string | null): { parts: string[]; shown: string | null } {
  const parts = node.output_kind === "multi" ? (node.schema ?? []).map((c) => c.name) : [];
  if (!parts.length) return { parts, shown: null };
  if (picked !== null && parts.includes(picked)) return { parts, shown: picked };
  return { parts, shown: (node.chart ? chartedPart(node) : null) ?? parts[0] };
}

/** A node with several outputs shows one at a time (outputPart), and `pick` changes which. */
export function useOutputPart(node: GraphNode): { parts: string[]; shown: string | null; pick: (part: string) => void } {
  const [picked, pick] = useState<string | null>(null);
  return { ...outputPart(node, picked), pick };
}

/** Which of a node's outputs is shown: one segment each. */
export function OutputPicker({ node, parts, shown, onPick }: { node: GraphNode; parts: string[]; shown: string; onPick: (part: string) => void }) {
  return (
    <SegmentedControl size="sm" value={shown} onChange={onPick} label={`${node.name}: outputs`}>
      {parts.map((name) => (
        <SegmentedControlItem key={name} value={name} label={name} />
      ))}
    </SegmentedControl>
  );
}

/** Whether a column's values are numbers (written at the end of their cells, in tabular figures), by its SQL type. */
export const isNumeric = (type: string) => /^(u?(tiny|small|big|huge)?int(eger|\d+)?$|float|double|decimal|numeric|real)/i.test(type);

/** The widest a cell is, in the mono's characters: past it a cell is cut short (`grid.cell`). */
const CELL_CHARS = 44;
/** Room for a cell's padding, in characters. */
const CHARS_PAD = 4;
/** How many characters of a cell count towards its column's width. */
const CHARS_CAP = CELL_CHARS - CHARS_PAD;

/** The header of a table: each column's name, its type and, with `stats`, its quick insights. */
function Head({ columns, numeric, stats, chars, ref }: { columns: Column[]; numeric: boolean[]; stats?: TableStats | null; chars?: number[]; ref?: Ref<HTMLTableSectionElement> }) {
  // Each column's quick insights (its missing values and range), from the footers.
  const insights = useMemo(() => new Map((stats?.columns ?? []).map((c) => [c.name, c])), [stats]);
  return (
    <thead ref={ref}>
      <tr>
        {columns.map((c, j) => (
          <th
            key={c.name}
            {...stylex.props(
              grid.cell,
              grid.head,
              numeric[j] && grid.num,
              stats && grid.withInsights,
              chars && (stats ? grid.charsWithInsights : grid.chars)(Math.min(chars[j] ?? 0, CHARS_CAP) + CHARS_PAD),
            )}
          >
            {c.name}
            <span {...stylex.props(grid.type)}>{c.type.toLowerCase()}</span>
            {stats && <Insights col={insights.get(c.name)} rows={stats.rows} />}
          </th>
        ))}
      </tr>
    </thead>
  );
}

/**
 * A table of an output or a query's result: a plain table, in the mono, whose
 * header stays in view as its scroller (the element around it) scrolls. Each
 * column is as wide as what is in it, up to a width past which a cell is cut
 * short and shows whole when pointed at. Every row is drawn: for a few
 * hundred at most (RowsTable draws any number).
 */
export function DataTable({ columns, rows, stats }: { columns: Column[]; rows: unknown[][]; stats?: TableStats | null }) {
  const numeric = useMemo(() => columns.map((c) => isNumeric(c.type)), [columns]);
  // Thousands of cells: their styles are worked out once, not per cell.
  const row = stylex.props(grid.row);
  const cell = stylex.props(grid.cell);
  const num = stylex.props(grid.cell, grid.num);
  return (
    <table {...stylex.props(grid.table)}>
      <Head columns={columns} numeric={numeric} stats={stats} />
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} {...row}>
            {r.map((v, j) => (
              <td key={j} {...(numeric[j] ? num : cell)} title={typeof v === "string" ? v : undefined}>
                {fmtCell(v)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Where a long table's rows come from: how many there are, row `i` (undefined
 * while it is on its way), how many characters wide each column's cells have
 * been so far, and `want`, told which rows are in view so they can be fetched.
 */
export interface RowSource {
  total: number;
  row: (i: number) => unknown[] | undefined;
  chars: number[];
  want?: (start: number, end: number) => void;
}

/** Rows already here (a chart's selection), as a RowSource. */
export function rowsSource(columns: Column[], rows: unknown[][]): RowSource {
  return { total: rows.length, row: (i) => rows[i], chars: columnChars(rows, fmtCell, headChars(columns), CHARS_CAP) };
}

const headChars = (columns: Column[]) => columns.map((c) => Math.max(c.name.length, c.type.length));

/** Pages of an output held at once: 20,000 rows, the ones nearest the rows in view. */
const HELD_PAGES = 40;

/**
 * Every row of a table output (or of one output, `part`), a page at a time:
 * `first` is its first page (useNodePreview with PAGE_ROWS), and the others
 * are read as the rows they hold are asked for (`want`), each from the same
 * run as the first so the pages agree. The pages furthest from the rows in
 * view are let go, so any number of rows can be scrolled through. Null until
 * `first` is a table.
 */
export function useRowPages(node: GraphNode, part: string | null | undefined, first: Preview | null): RowSource | null {
  const [, redraw] = useReducer((n: number) => n + 1, 0);
  const table = first?.kind === "table" && first.columns && first.rows ? first : null;
  const of = table ? `${node.name}/${part ?? ""}/${table.run_id ?? ""}/${table.total ?? ""}` : null;
  // The pages of one output of one run; another output starts with nothing but its first page.
  const store = useMemo(
    () =>
      table && {
        pages: new Map<number, unknown[][]>([[0, table.rows!]]),
        asked: new Set<number>(),
        chars: columnChars(table.rows!, fmtCell, headChars(table.columns!), CHARS_CAP),
        inView: [0, 0] as [number, number],
        live: true,
      },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [of],
  );
  useEffect(() => {
    if (!store) return;
    store.live = true;
    return () => {
      store.live = false;
    };
  }, [store]);
  const name = node.name;
  const runId = table?.run_id;
  const want = useCallback(
    (start: number, end: number) => {
      if (!store) return;
      const pages = pagesFor(start, end);
      if (!pages.length) return;
      store.inView = [pages[0], pages[pages.length - 1]];
      for (const page of pages) {
        if (store.pages.has(page) || store.asked.has(page)) continue;
        store.asked.add(page);
        api.rows(name, { offset: page * PAGE_ROWS, limit: PAGE_ROWS, part: part ?? undefined, runId }).then(
          (rows) => {
            store.asked.delete(page);
            if (!store.live) return;
            store.pages.set(page, rows);
            store.chars = columnChars(rows, fmtCell, store.chars, CHARS_CAP);
            for (const far of pagesToDrop([...store.pages.keys()], ...store.inView, HELD_PAGES)) store.pages.delete(far);
            redraw();
          },
          // Not read (the run's output is gone, the server is away): asked for again when its rows next come into view.
          () => void store.asked.delete(page),
        );
      }
    },
    [store, name, part, runId],
  );
  if (!store || !table) return null;
  return {
    total: table.total ?? table.rows!.length,
    row: (i) => store.pages.get(Math.floor(i / PAGE_ROWS))?.[i % PAGE_ROWS],
    chars: store.chars,
    want,
  };
}

/** A row's height until one is measured, in px. */
const ROW_HEIGHT = 27;
/** How long the rows in view stay the same before the ones missing are fetched, in ms: a drag of the scrollbar passes pages by. */
const FETCH_AFTER = 80;

/**
 * A table of any number of rows, in a scroller of its own: only the rows in
 * view are drawn (rowWindow), with empty space standing for the rest, and
 * `source` is told which rows those are so it can fetch them. A row that is
 * not here yet shows as a line of `…`. A column never narrows as rows scroll
 * through it (`source.chars`). `resetKey` changes when other rows are shown:
 * the table goes back to its top.
 */
export function RowsTable({ columns, source, stats, resetKey }: { columns: Column[]; source: RowSource; stats?: TableStats | null; resetKey?: unknown }) {
  const scroller = useRef<HTMLDivElement>(null);
  const head = useRef<HTMLTableSectionElement>(null);
  const firstRow = useRef<HTMLTableRowElement>(null);
  const [view, setView] = useState({ top: 0, height: 600, head: 0, row: ROW_HEIGHT });
  const numeric = useMemo(() => columns.map((c) => isNumeric(c.type)), [columns]);
  const { total } = source;

  // The scroller's height and the header's (taller once the insights come), as they change.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const measure = () => {
      const height = el.clientHeight;
      const headHeight = head.current?.offsetHeight ?? 0;
      setView((v) => (v.height === height && v.head === headHeight ? v : { ...v, height, head: headHeight }));
    };
    measure();
    const seen = new ResizeObserver(measure);
    seen.observe(el);
    if (head.current) seen.observe(head.current);
    return () => seen.disconnect();
  }, []);

  // Other rows: back to the top.
  useLayoutEffect(() => {
    if (scroller.current) scroller.current.scrollTop = 0;
    setView((v) => (v.top === 0 ? v : { ...v, top: 0 }));
  }, [resetKey]);

  const win = rowWindow({ scrollTop: view.top, viewport: view.height - view.head, rowHeight: view.row, total });

  // A row's real height, from the first one drawn.
  useLayoutEffect(() => {
    const h = firstRow.current?.getBoundingClientRect().height;
    if (h && Math.abs(h - view.row) > 0.5) setView((v) => ({ ...v, row: h }));
  });

  const { want } = source;
  useEffect(() => {
    if (!want) return;
    const timer = setTimeout(() => want(win.start, win.end), FETCH_AFTER);
    return () => clearTimeout(timer);
  }, [want, win.start, win.end]);

  const onScroll = (e: { currentTarget: HTMLDivElement }) => {
    const at = e.currentTarget.scrollTop;
    setView((v) => {
      // While a row is where its number puts it, only a scroll past a row draws again.
      const top = total * v.row <= MAX_ROWS_HEIGHT ? Math.floor(at / v.row) * v.row : at;
      return v.top === top ? v : { ...v, top };
    });
  };

  const row = stylex.props(grid.row);
  const cell = stylex.props(grid.cell);
  const num = stylex.props(grid.cell, grid.num);
  const waiting = stylex.props(grid.cell, grid.waiting);
  const pad = stylex.props(grid.pad);
  const rows = [];
  for (let i = win.start; i < win.end; i++) {
    const r = source.row(i);
    rows.push(
      <tr key={i} ref={i === win.start ? firstRow : undefined} aria-rowindex={i + 2} {...row}>
        {r ? (
          r.map((v, j) => (
            <td key={j} {...(numeric[j] ? num : cell)} title={typeof v === "string" ? v : undefined}>
              {fmtCell(v)}
            </td>
          ))
        ) : (
          <td colSpan={columns.length} {...waiting}>
            …
          </td>
        )}
      </tr>,
    );
  }
  return (
    <div ref={scroller} onScroll={onScroll} {...stylex.props(grid.scroller)}>
      <table aria-rowcount={total + 1} {...stylex.props(grid.table)}>
        <Head ref={head} columns={columns} numeric={numeric} stats={stats} chars={source.chars} />
        <tbody>
          {win.padTop > 0 && (
            <tr aria-hidden {...stylex.props(grid.space(win.padTop))}>
              <td colSpan={columns.length} {...pad} />
            </tr>
          )}
          {rows}
          {win.padBottom > 0 && (
            <tr aria-hidden {...stylex.props(grid.space(win.padBottom))}>
              <td colSpan={columns.length} {...pad} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/** A string bound as the grid shows a cell, with a time's `T` as a space. */
const bound = (v: unknown) => (typeof v === "string" && /^\d{4}-\d\d-\d\dT\d/.test(v) ? fmtCell(v).replace("T", " ") : fmtCell(v));

/**
 * A column header's quick insights, laid out as Data Wrangler lays them out
 * (a label at the start, its value at the end): how many values are missing,
 * and the smallest and largest. Only what the footers say: what they do not
 * (a nested column, a writer that kept no statistics) is left out, not
 * guessed, and nothing that needs the rows (distinct values, a histogram).
 */
function Insights({ col, rows }: { col: ColumnStats | undefined; rows: number }) {
  if (!col) return null;
  const min = col.min == null ? null : bound(col.min);
  const max = col.max == null ? null : bound(col.max);
  return (
    <>
      {col.nulls != null && (
        <span {...stylex.props(grid.insight)}>
          <span {...stylex.props(grid.side, grid.label)}>Missing:</span>
          <span {...stylex.props(grid.side, grid.end)}>{`${fmtNum(col.nulls)}${share(col.nulls, rows)}`}</span>
        </span>
      )}
      {(
        [
          ["Min:", min],
          ["Max:", max],
        ] as const
      ).map(
        ([label, v]) =>
          v != null && (
            <span key={label} {...stylex.props(grid.insight)} title={`${label} ${v}`}>
              <span {...stylex.props(grid.side, grid.label)}>{label}</span>
              <span {...stylex.props(grid.side, grid.end)}>{v}</span>
            </span>
          ),
      )}
    </>
  );
}

/**
 * A chart's series colours: the accent first, then the states' hues and four
 * more, as the theme has them (its custom properties, so they follow the mode).
 */
const SERIES = [
  "--color-accent",
  "--color-warning",
  "--color-success",
  "--color-error",
  "--color-data-categorical-pink",
  "--color-data-categorical-teal",
  "--color-data-categorical-purple",
  "--color-data-neutral",
];

/** What the theme's colours are at `host`, here and now, in a form a canvas can paint with. */
export function themeColors(host: HTMLElement, names: string[]): string[] {
  const probe = document.createElement("span");
  host.appendChild(probe);
  const out = names.map((name) => {
    probe.style.color = `var(${name})`;
    return getComputedStyle(probe).color;
  });
  probe.remove();
  return out;
}

/**
 * What is selected on a chart: the charted rows inside it (`rows`, of the
 * `of` charted), the same selection as a SQL condition over the whole
 * output (`where`), and whether the charted rows are the whole output
 * (`complete`: then `rows` is the answer and no query is needed). Null while
 * nothing is selected.
 */
export type ChartSelection = { columns: Column[]; rows: unknown[][]; of: number; where: string; complete: boolean } | null;

type Enc = { field?: unknown; type?: unknown; aggregate?: unknown; bin?: unknown; timeUnit?: unknown } | undefined;

/** A row's value on a continuous axis, as a number (a time as its milliseconds). */
const asNumber = (v: unknown): number => (typeof v === "number" ? v : v instanceof Date ? v.getTime() : typeof v === "string" ? Date.parse(v) : NaN);

/**
 * The selection a chart gets so it can pick rows, or null when its spec is
 * not one it can be added to (a layered, faceted or concatenated chart, one
 * with params of its own, or one whose x is aggregated, binned or by time
 * unit, where a mark is not a row). A continuous x is brushed by dragging (x
 * and y for points, x alone otherwise); a category x is picked by clicking.
 */
export function chartBrush(chart: Record<string, unknown> | null): { param: Record<string, unknown>; fields: string[]; kind: "interval" | "point" } | null {
  if (!chart || ["layer", "facet", "repeat", "concat", "hconcat", "vconcat", "params", "selection"].some((k) => k in chart)) return null;
  const enc = (chart.encoding ?? {}) as Record<string, Enc>;
  const plain = (e: Enc) => !!e && typeof e.field === "string" && !e.aggregate && !e.bin && !e.timeUnit;
  const continuous = (e: Enc) => plain(e) && (e!.type === "quantitative" || e!.type === "temporal");
  const { x, y } = enc;
  if (!plain(x)) return null;
  const mark = typeof chart.mark === "string" ? chart.mark : (chart.mark as { type?: string } | undefined)?.type;
  if (continuous(x)) {
    const both = ["point", "circle", "square"].includes(mark ?? "") && continuous(y);
    const fields = both ? [x!.field as string, y!.field as string] : [x!.field as string];
    return { kind: "interval", fields, param: { name: "brush", select: { type: "interval", encodings: both ? ["x", "y"] : ["x"] } } };
  }
  return { kind: "point", fields: [x!.field as string], param: { name: "brush", select: { type: "point", encodings: ["x"] } } };
}

/** The rows of `rows` (named by `columns`) inside a selection's value (Vega's `brush` signal); null for an empty one. */
export function selectedRows(
  brush: { kind: "interval" | "point"; fields: string[] },
  value: Record<string, unknown> | null | undefined,
  columns: Column[],
  rows: unknown[][],
): unknown[][] | null {
  if (!value) return null;
  const tests: ((r: unknown[]) => boolean)[] = [];
  for (const f of brush.fields) {
    const got = value[f];
    const i = columns.findIndex((c) => c.name === f);
    if (!Array.isArray(got) || i < 0) continue;
    if (brush.kind === "interval") {
      const [lo, hi] = [asNumber(got[0]), asNumber(got[1])].sort((a, b) => a - b);
      tests.push((r) => {
        const v = asNumber(r[i]);
        return v >= lo && v <= hi;
      });
    } else {
      const keep = new Set(got.map(String));
      tests.push((r) => keep.has(String(r[i])));
    }
  }
  if (!tests.length) return null;
  return rows.filter((r) => tests.every((t) => t(r)));
}

const ident = (name: string) => `"${name.replace(/"/g, '""')}"`;
const literal = (v: unknown) => `'${String(v).replace(/'/g, "''")}'`;

/**
 * A selection's value (as `selectedRows` reads it) as a SQL condition on the
 * output's columns, keeping what `selectedRows` keeps. A time on the chart is
 * the instant the browser read a cell's text as: a date is midnight UTC, a
 * time without a zone is local time, one with a zone is that instant. Null
 * for an empty selection.
 */
export function selectionWhere(brush: { kind: "interval" | "point"; fields: string[] }, value: Record<string, unknown> | null | undefined, columns: Column[]): string | null {
  if (!value) return null;
  const parts: string[] = [];
  for (const f of brush.fields) {
    const got = value[f];
    const col = columns.find((c) => c.name === f);
    if (!Array.isArray(got) || !col) continue;
    const c = ident(f);
    if (brush.kind === "point") {
      parts.push(`cast(${c} as varchar) in (${got.map(literal).join(", ")})`);
      continue;
    }
    const [lo, hi] = [asNumber(got[0]), asNumber(got[1])].sort((a, b) => a - b);
    const type = col.type.toUpperCase();
    if (/WITH TIME ZONE|TIMESTAMPTZ/.test(type)) parts.push(`${c} between to_timestamp(${lo / 1000}) and to_timestamp(${hi / 1000})`);
    else if (type === "DATE") parts.push(`cast(${c} as timestamp) between make_timestamp(${Math.round(lo * 1000)}) and make_timestamp(${Math.round(hi * 1000)})`);
    else if (/TIMESTAMP|DATETIME/.test(type)) {
      // A local instant back to the wall-clock time the cell holds.
      const wall = (ms: number) => Math.round((ms - new Date(ms).getTimezoneOffset() * 60_000) * 1000);
      parts.push(`${c} between make_timestamp(${wall(lo)}) and make_timestamp(${wall(hi)})`);
    } else parts.push(`${c} between ${lo} and ${hi}`);
  }
  return parts.length ? parts.join(" and ") : null;
}

/** What a selection holds, in words: in the whole output once that is read, of the charted rows until then. */
export function SelectionText({ sel, picked }: { sel: NonNullable<ChartSelection>; picked: NonNullable<ReturnType<typeof useSelectedRows>> }) {
  if (picked.whole && picked.total != null)
    return (
      <>
        {picked.rows.length < picked.total ? `First ${fmtNum(picked.rows.length)} of ` : ""}
        {fmtNum(picked.total)} selected row{picked.total === 1 ? "" : "s"}
      </>
    );
  return (
    <>
      {fmtNum(sel.rows.length)} of {fmtNum(sel.of)} charted rows selected
      {picked.failed ? " (the rest could not be read)" : ", counting all…"}
    </>
  );
}

/** How many of a selection's rows are read from the whole output, to show. */
export const SELECTED_ROWS = 1000;

/**
 * The rows a chart's selection holds in the whole output, not only in the
 * rows charted: the charted ones at once, then (once the selection has
 * stopped moving) the first `SELECTED_ROWS` of the whole output's and how
 * many there are, read with SQL. `total` is null while that count is on its
 * way; `failed` says it could not be read (the charted rows stay).
 */
export function useSelectedRows(
  node: GraphNode,
  part: string | null,
  sel: ChartSelection,
): { columns: Column[]; rows: unknown[][]; total: number | null; whole: boolean; failed: boolean } | null {
  const [got, setGot] = useState<{ where: string; columns: Column[]; rows: unknown[][]; total: number } | { where: string; failed: true } | null>(null);
  useEffect(() => {
    if (!sel || sel.complete) return;
    let live = true;
    const from = ident(part ? `${node.name}.${part}` : node.name);
    const where = sel.where;
    // A drag moves the selection many times a second: the whole output is read once it rests.
    const timer = setTimeout(() => {
      Promise.all([api.query(`select count(*) as n from ${from} where ${where}`, 1), api.query(`select * from ${from} where ${where}`, SELECTED_ROWS)]).then(
        ([count, page]) => live && setGot({ where, columns: page.columns, rows: page.rows, total: Number(count.rows[0]?.[0] ?? 0) }),
        () => live && setGot({ where, failed: true }),
      );
    }, 300);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [node.name, node.artifact_id, part, sel?.where, sel?.complete]);
  if (!sel) return null;
  if (sel.complete) return { columns: sel.columns, rows: sel.rows.slice(0, SELECTED_ROWS), total: sel.rows.length, whole: true, failed: false };
  if (got && got.where === sel.where) {
    if ("failed" in got) return { columns: sel.columns, rows: sel.rows.slice(0, SELECTED_ROWS), total: null, whole: false, failed: true };
    return { columns: got.columns, rows: got.rows, total: got.total, whole: true, failed: false };
  }
  return { columns: sel.columns, rows: sel.rows.slice(0, SELECTED_ROWS), total: null, whole: false, failed: false };
}

/** How many rows a chart draws: every row of an output up to this many, else a sample this size of all of them. */
export const CHART_ROWS = 50_000;

/**
 * The rows a chart draws: every row of the output (or of `part`) when it
 * has at most CHART_ROWS, else a random sample of CHART_ROWS from all of
 * them (the same one each time), not its first rows. `sampled` says which.
 */
function useChartData(node: GraphNode, part: string | null): { data: Preview | null; sampled: boolean } {
  // A node with one output says how many rows it has: one that big is sampled from the start.
  const big = part === null && (node.rows ?? 0) > CHART_ROWS;
  const { preview } = useNodePreview(node, CHART_ROWS, part, !big);
  const sampled = big || (preview?.kind === "table" && (preview.total ?? 0) > (preview.rows?.length ?? 0));
  const [sample, setSample] = useState<{ key: string; data: Preview } | null>(null);
  const key = `${node.name}:${node.artifact_id}:${part}`;
  useEffect(() => {
    if (!sampled) return;
    let live = true;
    const from = ident(part ? `${node.name}.${part}` : node.name);
    const total = big ? node.rows : preview?.total;
    api.query(`select * from ${from} using sample ${CHART_ROWS} rows (reservoir, 1)`, CHART_ROWS).then(
      (r) => live && setSample({ key, data: { kind: "table", columns: r.columns, rows: r.rows, total } }),
      () => live && setSample({ key, data: { kind: null } }),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, sampled]);
  if (!sampled) return { data: preview, sampled: false };
  return { data: sample?.key === key ? sample.data : null, sampled: true };
}

/**
 * Under a chart linked to a table (`chartBrush`): how rows are picked on it,
 * or what is picked (`selected`), and Clear. Always the same height, so picking does
 * not move the table under it.
 */
export function ChartPick({ kind, selected, onClear }: { kind: "interval" | "point"; selected: ReactNode | null; onClear: () => void }) {
  return (
    <HStack gap={2} vAlign="center">
      <Text type="supporting" color="secondary" maxLines={1} hasTabularNumbers>
        {selected ?? `${kind === "interval" ? "Drag across" : "Click"} the chart to pick rows`}
      </Text>
      <Button size="sm" variant="ghost" label="Clear" isDisabled={selected == null} onClick={onClear} />
    </HStack>
  );
}

/**
 * A node's chart (`chart` in its decorator): its Vega-Lite spec over its
 * output, in the theme's colours. With `onSelect` it is linked to a table
 * (`chartBrush`); a new `reset` draws it again with nothing selected.
 */
export function NodeChart({ node, dark, height = 300, onSelect, reset = 0 }: { node: GraphNode; dark: boolean; height?: number; onSelect?: (s: ChartSelection) => void; reset?: number }) {
  // The latest onSelect, so a new one does not draw the chart again.
  const select = useRef(onSelect);
  select.current = onSelect;
  const linked = !!onSelect;
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  // A node with several outputs charts its first table.
  const { data: d, sampled } = useChartData(node, chartedPart(node));
  // The spec as text: the graph is read again whenever anything in it runs,
  // and the same chart must not be drawn again (and lose its selection) for that.
  const chartText = JSON.stringify(node.chart ?? null);
  useEffect(() => {
    if (!d || !ref.current) return;
    const chart = JSON.parse(chartText) as GraphNode["chart"];
    let cancelled = false;
    let finalize: (() => void) | undefined;
    let frame = 0;
    (async () => {
      if (d.kind !== "table" || !d.columns || !d.rows) {
        setError("Charts need a table output. Run the node first.");
        return;
      }
      const cols = d.columns.map((c) => c.name);
      const values = d.rows.map((r) => Object.fromEntries(r.map((v, i) => [cols[i], v])));
      // The chart is drawn on a canvas: it takes the theme's colours as they are now.
      const [label, rule, strong, ...series] = themeColors(ref.current!, ["--color-text-secondary", "--color-border", "--color-border-emphasized", ...SERIES]);
      // Linked to a table: a selection on the chart picks the rows it shows.
      const brush = linked ? chartBrush(chart) : null;
      const spec = {
        $schema: "https://vega.github.io/schema/vega-lite/v5.json",
        width: "container",
        height,
        autosize: { type: "fit", contains: "padding" },
        ...chart,
        ...(brush && {
          params: [brush.kind === "interval" ? { ...brush.param, select: { ...(brush.param.select as object), mark: { fill: series[0], fillOpacity: 0.12, stroke: series[0] } } } : brush.param],
        }),
        data: { values },
        config: {
          background: "transparent",
          font: getComputedStyle(ref.current!).fontFamily || "sans-serif",
          view: { stroke: null },
          axis: { labelColor: label, titleColor: label, gridColor: rule, domainColor: strong, tickColor: strong },
          legend: { labelColor: label, titleColor: label },
          range: { category: series },
          ...(chart?.config as object | undefined),
        },
      };
      try {
        setError(null);
        // Vega is as large as the rest of the app: it is fetched when a chart is first drawn.
        const { default: embed } = await import("vega-embed");
        if (cancelled || !ref.current) return;
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const res = await embed(ref.current, spec as any, { actions: { export: true, source: false, compiled: false, editor: false } });
        if (cancelled) {
          res.finalize();
          return;
        }
        finalize = () => res.finalize();
        select.current?.(null);
        if (brush) {
          // A drag fires on every move: the rows are worked out once a frame.
          res.view.addSignalListener("brush", (_name: string, value: unknown) => {
            cancelAnimationFrame(frame);
            frame = requestAnimationFrame(() => {
              const v = value as Record<string, unknown>;
              const rows = selectedRows(brush, v, d.columns!, d.rows!);
              const where = selectionWhere(brush, v, d.columns!);
              const complete = !sampled && d.rows!.length >= (d.total ?? d.rows!.length);
              select.current?.(rows && where ? { columns: d.columns!, rows, of: d.rows!.length, where, complete } : null);
            });
          });
        }
      } catch (e) {
        if (!cancelled) setError(`Chart spec error: ${(e as Error).message}`);
      }
    })();
    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
      finalize?.();
    };
  }, [d, sampled, chartText, dark, height, linked, reset]);
  return (
    <>
      {error && <Banner status="error" title={error} />}
      <div ref={ref} {...stylex.props(styles.vega)} />
      {sampled && d?.rows && (
        <Text type="supporting" color="secondary" hasTabularNumbers>
          A random sample of {fmtNum(d.rows.length)} of {fmtNum(d.total)} rows
          {linked ? "; a selection holds every row in it" : ""}
        </Text>
      )}
    </>
  );
}

const rule = {
  borderBottomWidth: "1px",
  borderBottomStyle: "solid",
  borderBottomColor: colorVars["--color-border"],
} as const;

const styles = stylex.create({
  // vega-embed makes its element inline; as a flex item it is as wide as its column, which the chart fits itself to.
  vega: { width: "100%", flexShrink: 0 },
});

/** The data table's parts (DataTable). */
const grid = stylex.create({
  table: {
    width: "max-content",
    minWidth: "100%",
    borderCollapse: "separate",
    borderSpacing: 0,
    fontFamily: typographyVars["--font-family-code"],
    fontSize: textSizeVars["--font-size-sm"],
    lineHeight: typeScaleVars["--text-body-leading"],
  },
  row: { backgroundColor: { default: "transparent", ":hover": colorVars["--color-background-muted"] } },
  cell: {
    maxWidth: `${CELL_CHARS}ch`,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    textAlign: "start",
    fontWeight: fontWeightVars["--font-weight-normal"],
    paddingBlock: spacingVars["--spacing-1"],
    // The first and the last column keep to the pane's own margin.
    paddingInlineStart: { default: spacingVars["--spacing-3"], ":first-child": spacingVars["--spacing-4"] },
    paddingInlineEnd: { default: spacingVars["--spacing-3"], ":last-child": spacingVars["--spacing-4"] },
    ...rule,
  },
  head: {
    position: "sticky",
    top: 0,
    zIndex: 1,
    fontWeight: fontWeightVars["--font-weight-semibold"],
    backgroundColor: colorVars["--color-background-muted"],
  },
  // A column's type, under its name.
  type: { display: "block", color: colorVars["--color-text-secondary"], fontWeight: fontWeightVars["--font-weight-normal"] },
  num: { textAlign: "end", fontVariantNumeric: "tabular-nums" },
  // An insight takes its column's width from the rows (width 0, then all of
  // it) instead of widening it; a long one is cut short and shows whole when pointed at.
  insight: {
    display: "flex",
    justifyContent: "space-between",
    gap: spacingVars["--spacing-2"],
    width: 0,
    minWidth: "100%",
    color: colorVars["--color-text-secondary"],
    fontWeight: fontWeightVars["--font-weight-normal"],
  },
  // As wide as Data Wrangler's columns, so the insights read whole.
  withInsights: { minWidth: "200px" },
  side: { minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  label: { flexShrink: 0 },
  end: { marginInlineStart: "auto", textAlign: "end" },
  // RowsTable: the pane its rows scroll in, as large as the room it is given.
  // Never so short that no row shows: a pane too small for that scrolls instead (the Data tab under its chart).
  scroller: { flexGrow: 1, minWidth: 0, minHeight: "200px", overflow: "auto" },
  // A column at least as wide as the widest cell it has held, in the mono's characters.
  chars: (n: number) => ({ minWidth: `${n}ch` }),
  charsWithInsights: (n: number) => ({ minWidth: `max(${n}ch, 200px)` }),
  // The space that stands for the rows over, or under, the ones drawn.
  space: (height: number) => ({ height: `${height}px` }),
  pad: { padding: 0, borderStyle: "none" },
  // A row that is on its way.
  waiting: { color: colorVars["--color-text-secondary"] },
});
