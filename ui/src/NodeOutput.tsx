import { useEffect, useMemo, useRef, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, fontWeightVars, spacingVars, textSizeVars, typeScaleVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { api, type Column, type GraphNode, type Preview } from "./api";
import { fmtCell } from "./util";

/*
 * What a node made, shown wherever it is shown: the inspector's Data and
 * Chart tabs, its query box, and a notebook cell's preview.
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
 * Which output of a node is shown: none for a node with one output; for one
 * with several, the one picked while it still has it, else its first.
 */
export function outputPart(node: GraphNode, picked: string | null): { parts: string[]; shown: string | null } {
  const parts = node.output_kind === "multi" ? (node.schema ?? []).map((c) => c.name) : [];
  return { parts, shown: parts.length ? (picked !== null && parts.includes(picked) ? picked : parts[0]) : null };
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

/**
 * A table of an output or a query's result: a plain table, in the mono, whose
 * header stays in view as its scroller (the element around it) scrolls. Each
 * column is as wide as what is in it, up to a width past which a cell is cut
 * short and shows whole when pointed at.
 */
export function DataTable({ columns, rows }: { columns: Column[]; rows: unknown[][] }) {
  const numeric = useMemo(
    () => columns.map((c) => /int|float|double|decimal|numeric|bigint|real/i.test(c.type)),
    [columns],
  );
  // Thousands of cells: their styles are worked out once, not per cell.
  const row = stylex.props(grid.row);
  const cell = stylex.props(grid.cell);
  const num = stylex.props(grid.cell, grid.num);
  return (
    <table {...stylex.props(grid.table)}>
      <thead>
        <tr>
          {columns.map((c, j) => (
            <th key={c.name} {...stylex.props(grid.cell, grid.head, numeric[j] && grid.num)}>
              {c.name}
              <span {...stylex.props(grid.type)}>{c.type.toLowerCase()}</span>
            </th>
          ))}
        </tr>
      </thead>
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

/** A node's chart (`chart` in its decorator): its Vega-Lite spec over its output, in the theme's colours. */
export function NodeChart({ node, dark, height = 300 }: { node: GraphNode; dark: boolean; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  // A node with several outputs charts its first table.
  const firstTable = node.output_kind === "multi" ? (node.schema ?? []).find((c) => c.type === "table")?.name : undefined;
  const { preview: d } = useNodePreview(node, 5000, firstTable);
  useEffect(() => {
    if (!d || !ref.current) return;
    let cancelled = false;
    let finalize: (() => void) | undefined;
    (async () => {
      if (d.kind !== "table" || !d.columns || !d.rows) {
        setError("Charts need a table output. Run the node first.");
        return;
      }
      const cols = d.columns.map((c) => c.name);
      const values = d.rows.map((r) => Object.fromEntries(r.map((v, i) => [cols[i], v])));
      // The chart is drawn on a canvas: it takes the theme's colours as they are now.
      const [label, rule, strong, ...series] = themeColors(ref.current!, ["--color-text-secondary", "--color-border", "--color-border-emphasized", ...SERIES]);
      const spec = {
        $schema: "https://vega.github.io/schema/vega-lite/v5.json",
        width: "container",
        height,
        autosize: { type: "fit", contains: "padding" },
        ...node.chart,
        data: { values },
        config: {
          background: "transparent",
          font: getComputedStyle(ref.current!).fontFamily || "sans-serif",
          view: { stroke: null },
          axis: { labelColor: label, titleColor: label, gridColor: rule, domainColor: strong, tickColor: strong },
          legend: { labelColor: label, titleColor: label },
          range: { category: series },
          ...(node.chart?.config as object | undefined),
        },
      };
      try {
        setError(null);
        // Vega is as large as the rest of the app: it is fetched when a chart is first drawn.
        const { default: embed } = await import("vega-embed");
        if (cancelled || !ref.current) return;
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const res = await embed(ref.current, spec as any, { actions: { export: true, source: false, compiled: false, editor: false } });
        if (cancelled) res.finalize();
        else finalize = () => res.finalize();
      } catch (e) {
        if (!cancelled) setError(`Chart spec error: ${(e as Error).message}`);
      }
    })();
    return () => {
      cancelled = true;
      finalize?.();
    };
  }, [d, node.chart, dark, height]);
  return (
    <>
      {error && <Banner status="error" title={error} />}
      <div ref={ref} {...stylex.props(styles.vega)} />
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
    maxWidth: "320px",
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
});
