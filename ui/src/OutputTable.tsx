import { useEffect, useMemo, useRef, useState } from "react";
import { EditorView } from "@uiw/react-codemirror";
import { sql as sqlLanguage } from "@codemirror/lang-sql";
import * as stylex from "@stylexjs/stylex";
import { colorVars, radiusVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Code } from "@astryxdesign/core/Code";
import { IconButton } from "@astryxdesign/core/IconButton";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Token } from "@astryxdesign/core/Token";
import { api, type GraphNode, type Preview, type TableStats } from "./api";
import { ago, fmtNum } from "./util";
import { IconCode, MOD } from "./icons";
import { DownloadMenu, tableItems } from "./Export";
import { nodeExportUrl, saveQuery, saveUrl } from "./download";
import { CodeEditor } from "./ui/CodeEditor";
import { oneOf, usePersisted } from "./ui/usePersisted";
import { chartBrush, ChartPick, RowsTable, rowsSource, useAskedRows, useRowPages, type ChartSelection } from "./NodeOutput";
import { baseQuery, filterText, filterWhere, isNumeric, nextSort, tableOf, withFilter, type ColumnFilter, type Sort, type TableAsk } from "./tableQuery";

/*
 * A table output's rows as the Data tab shows them, wherever they are shown
 * (the Data tab, a notebook cell, a chart full screen): every row, drawn as
 * it scrolls into view and read a page at a time, which can be asked for
 * another way (tableQuery.ts): filtered by a SQL condition, typed or (with
 * `canPrompt`: a model is set up and the person may spend it) written by the
 * model from a prompt, or a column at a time (the funnel in its header),
 * sorted by a column (its header is pressed), picked on the chart, or as a
 * query of one's own over the output.
 *
 * `useOutputTable` holds what is asked; the host draws the chart (its
 * selection is `t.sel`, `t.setSel`) with `OutputChartPick` under it, and
 * `OutputRows` where the rows go, in a column that gives them room.
 */

/** How the filter is written: a SQL condition, or a prompt the model writes the condition for. */
type FilterBy = "sql" | "prompt";
const FILTER_BY = oneOf<FilterBy>("sql", "prompt");

/** The query box's language, and its name for a screen reader. */
export const QUERY = [sqlLanguage(), EditorView.contentAttributes.of({ "aria-label": "SQL over the nodes' outputs" })];

export type OutputTable = ReturnType<typeof useOutputTable>;

/**
 * What is asked of a node's table output (`shown`, one of its outputs, or
 * null for its only one), and the rows that answer it. `preview` is the
 * output's first page (useNodePreview with PAGE_ROWS); `isTable` says the
 * output is a table, so it can be asked of (before its preview is here: a
 * chart's selection is counted at once). Another node or output: nothing is
 * asked.
 */
export function useOutputTable(node: GraphNode, shown: string | null, preview: Preview | null, { canPrompt = false, isTable }: { canPrompt?: boolean; isTable: boolean }) {
  // What is selected on the chart; a new `cleared` draws it again, unselected.
  const [sel, setSel] = useState<ChartSelection>(null);
  const [cleared, setCleared] = useState(0);
  // What is asked of the rows: a condition (`filter`, as applied; `draft` as
  // it is typed), filters of single columns (`byColumn`), a column to sort
  // by, or a query written out (`sql` as run, null while none is written;
  // `sqlDraft` as it is typed).
  const [filter, setFilter] = useState("");
  const [draft, setDraft] = useState("");
  const [byColumn, setByColumn] = useState<ColumnFilter[]>([]);
  const [sort, setSort] = useState<Sort | null>(null);
  const [sql, setSql] = useState<string | null>(null);
  const [sqlDraft, setSqlDraft] = useState("");
  const writing = sql !== null;
  // How the filter is written: as SQL, or as a prompt the model makes the
  // SQL of (`words` as typed; `prompted`, the prompt and the condition it
  // became, while that is the filter; `prompting`, the one the model is
  // writing now). The browser remembers which.
  const [kept, setKept] = usePersisted<FilterBy>("exuvia-filter-by", "sql", FILTER_BY);
  const by: FilterBy = canPrompt ? kept : "sql";
  const [words, setWords] = useState("");
  const [prompted, setPrompted] = useState<{ words: string; where: string } | null>(null);
  const [prompting, setPrompting] = useState<string | null>(null);
  const [promptError, setPromptError] = useState<string | null>(null);
  // The latest prompt sent: an answer to an earlier one, or for another table, is dropped.
  const promptSeq = useRef(0);
  const noPrompt = () => {
    promptSeq.current += 1;
    setPrompting(null);
    setPromptError(null);
  };
  // Another output, or another node: nothing is selected or asked.
  useEffect(() => {
    setSel(null);
    setFilter("");
    setDraft("");
    setByColumn([]);
    setSort(null);
    setSql(null);
    setWords("");
    setPrompted(null);
    noPrompt();
  }, [node.name, shown]);
  // The model writes the condition for `words`: it becomes the filter, and the SQL box holds it to read and change.
  const prompt = async () => {
    const text = words.trim();
    if (!text) return;
    const seq = ++promptSeq.current;
    setPrompting(text);
    setPromptError(null);
    try {
      const where = await api.filterFromWords(node.name, text, shown);
      if (promptSeq.current !== seq) return;
      setFilter(where);
      setDraft(where);
      setPrompted({ words: text, where });
    } catch (e) {
      if (promptSeq.current !== seq) return;
      setPromptError((e as Error).message);
    }
    setPrompting(null);
  };
  // A query of one's own is not the chart's rows: the chart picks nothing while one is written.
  const brush = !writing ? chartBrush(node.chart) : null;
  // Every row of the output, its first page here and the rest read as they scroll into view.
  const all = useRowPages(node, shown, preview);
  // The rows asked for, when anything is: read with SQL, a page at a time too.
  const table = tableOf(node.name, shown);
  const picked = sel?.where ?? "";
  // Each column's filter as a condition, by the column's type in the output; one whose column the output no longer has is left out.
  const columns = preview?.kind === "table" ? preview.columns : undefined;
  const columnFilters = useMemo(
    () =>
      byColumn.flatMap((f) => {
        const type = columns?.find((c) => c.name === f.column)?.type;
        if (type === undefined) return [];
        const where = filterWhere(f, type);
        return where ? [{ filter: f, where, text: filterText(f, type) }] : [];
      }),
    [byColumn, columns],
  );
  const conditions = useMemo(() => [filter, ...columnFilters.map((f) => f.where)], [filter, columnFilters]);
  const ask = useMemo<TableAsk>(() => ({ sql, where: [...conditions, picked], sort }), [sql, conditions, picked, sort]);
  const asked = useAskedRows(table, ask, node.artifact_id, isTable);
  const filtered = conditions.some(Boolean);
  // The charted rows a selection holds show at once, until all of its rows are read.
  const quick = useMemo(() => (sel && !filtered && !sort && !writing ? rowsSource(sel.columns, sel.rows) : null), [sel, filtered, sort, writing]);
  const clearSel = () => {
    setSel(null);
    setCleared((n) => n + 1);
  };
  return {
    node,
    shown,
    preview,
    canPrompt,
    sel,
    // While a query is written the chart picks nothing.
    onSelect: writing ? undefined : setSel,
    cleared,
    clearSel,
    brush,
    filter,
    setFilter,
    draft,
    setDraft,
    byColumn,
    setByColumn,
    sort,
    setSort,
    sql,
    setSql,
    sqlDraft,
    setSqlDraft,
    writing,
    by,
    setKept,
    words,
    setWords,
    prompted,
    setPrompted,
    prompting,
    promptError,
    setPromptError,
    noPrompt,
    prompt,
    all,
    table,
    columnFilters,
    conditions,
    asked,
    filtered,
    quick,
  };
}

/** What is selected on the chart, in words: the rows that answer it once they are counted, the charted ones until then. */
function selectedText(t: OutputTable): string | null {
  const { sel, asked, filtered } = t;
  if (!sel) return null;
  if (asked.current && asked.table) return `${fmtNum(asked.table.total)} row${asked.table.total === 1 ? "" : "s"} selected${filtered ? " that pass the filter" : ""}`;
  return `${fmtNum(sel.rows.length)} of ${fmtNum(sel.of)} charted rows selected${asked.error ? " (the rest could not be read)" : ", counting all…"}`;
}

/** Under the chart: how it picks rows, what it has picked, and Clear. Nothing for a chart that cannot pick rows. */
export function OutputChartPick({ t }: { t: OutputTable }) {
  if (!t.brush) return null;
  return <ChartPick kind={t.brush.kind} selected={selectedText(t)} onClear={t.clearSel} />;
}

/**
 * The rows of `t`, and what is asked of them: the filter (or the query
 * being written) over them, the columns filtered, and a bar under them with
 * how many there are and Download. Laid out in its host's column: the rows
 * take the room the rest leaves them. `stats` are the output's quick
 * insights (useTableStats).
 */
export function OutputRows({ t, dark, stats }: { t: OutputTable; dark: boolean; stats: TableStats | null }) {
  const { node, shown, preview: p, all, asked, quick, sel, writing, sort, by, filter, columnFilters, conditions, table, filtered } = t;
  if (!(p?.kind === "table" && p.columns && p.rows && all))
    return (
      <div {...stylex.props(styles.center)}>
        <Spinner aria-label="Loading the rows" />
      </div>
    );
  // What the table shows: the rows asked for once they are read; until
  // then the charted rows of a selection, else what it showed before.
  const answer = asked.table && (asked.current || !quick) ? asked.table : null;
  const view = answer
    ? { columns: answer.columns, source: answer.source, key: answer.sql as unknown }
    : quick && sel
      ? { columns: sel.columns, source: quick, key: sel as unknown }
      : { columns: p.columns, source: all, key: `${node.artifact_id}/${shown ?? ""}` as unknown };
  const narrowed = filtered || !!sel;
  const runSql = () => {
    t.setSort(null);
    t.setSql(t.sqlDraft);
  };
  const example = p.columns.find((c) => isNumeric(c.type));
  // Whether the filter is the condition the model wrote for the last prompt (not one typed since).
  const ofPrompt = t.prompted !== null && t.prompted.where === filter;
  const zipAll =
    node.output_kind === "multi"
      ? [{ items: [{ key: "zip", label: "All outputs (.zip)", hint: "every output as it is stored", onPick: () => saveUrl(nodeExportUrl(node.name, "zip")) }] }]
      : [];
  return (
    <>
      {writing ? (
        <div {...stylex.props(styles.ask, styles.askSql)}>
          <div
            {...stylex.props(styles.query)}
            onKeyDownCapture={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                e.preventDefault();
                e.stopPropagation();
                runSql();
              }
            }}
          >
            <CodeEditor value={t.sqlDraft} onChange={t.setSqlDraft} dark={dark} activeLine={false} extensions={QUERY} />
          </div>
          <div {...stylex.props(styles.queryRow)}>
            <Button size="sm" label="Run" isLoading={asked.loading} onClick={runSql} />
            <Button size="sm" variant="ghost" label="Back to the rows" tooltip="Leave the query: the output's own rows again" onClick={() => t.setSql(null)} />
            <Text type="supporting" color="secondary" maxLines={1}>
              DuckDB · every table node is a view · {MOD}Enter
            </Text>
          </div>
        </div>
      ) : (
        <div {...stylex.props(styles.ask)}>
          {t.canPrompt && (
            <SegmentedControl
              size="sm"
              label="Write the filter as"
              value={by}
              onChange={(v) => {
                t.setKept(v as FilterBy);
                t.setPromptError(null);
              }}
            >
              <SegmentedControlItem value="sql" label="SQL" />
              <SegmentedControlItem value="prompt" label="Prompt" />
            </SegmentedControl>
          )}
          {by === "prompt" ? (
            <TextInput
              size="sm"
              type="search"
              label="Filter the rows with a prompt"
              isLabelHidden
              placeholder={`Filter: say which rows, e.g. ${example ? `the largest ${example.name}` : "the latest ones"} (Enter)`}
              value={t.words}
              onChange={(v) => {
                t.setWords(v);
                if (!v) {
                  // cleared: every row again, without a press of Enter
                  t.noPrompt();
                  t.setPrompted(null);
                  t.setFilter("");
                  t.setDraft("");
                }
              }}
              onEnter={t.prompt}
              hasClear
              autoComplete="off"
              isLoading={t.prompting !== null || asked.loading}
              status={t.promptError || asked.error ? { type: "error" } : t.words.trim() !== (t.prompting ?? (ofPrompt ? t.prompted!.words : "")) ? { type: "warning" } : undefined}
              containerXstyle={styles.askInput}
            />
          ) : (
            <TextInput
              size="sm"
              type="search"
              label="Filter the rows with a SQL condition"
              isLabelHidden
              placeholder={`Filter: a SQL condition, e.g. ${example ? `${example.name} > 0` : `${p.columns[0]?.name ?? "column"} = '…'`} (Enter)`}
              value={t.draft}
              onChange={(v) => {
                t.setDraft(v);
                if (!v) t.setFilter(""); // cleared: every row again, without a press of Enter
              }}
              onEnter={() => t.setFilter(t.draft.trim())}
              hasClear
              hasSpellCheck={false}
              autoComplete="off"
              autoCapitalize="off"
              isLoading={asked.loading}
              status={asked.error ? { type: "error" } : t.draft.trim() !== filter ? { type: "warning" } : undefined}
              containerXstyle={styles.askInput}
            />
          )}
          <IconButton
            size="sm"
            variant="ghost"
            label="Write a SQL query"
            tooltip="Write a SQL query of your own over this output"
            icon={<IconCode />}
            onClick={() => {
              const text = baseQuery(table, { sql: null, where: conditions, sort: null });
              t.setSqlDraft(text);
              t.setSort(null);
              t.clearSel();
              t.setSql(text);
            }}
          />
          {by === "prompt" && filter && (
            <Text type="supporting" color="secondary" maxLines={2} xstyle={styles.askWhere}>
              {ofPrompt ? "Filtered where " : "Filtered, by a condition typed as SQL, where "}
              <Code size="inherit">{filter}</Code>
            </Text>
          )}
        </div>
      )}
      {t.promptError && by === "prompt" && !writing && <Banner status="error" title={t.promptError} />}
      {!writing && columnFilters.length > 0 && (
        <div {...stylex.props(styles.strip)}>
          {columnFilters.map(({ filter: f, text }) => (
            <Token key={f.column} size="sm" label={text} onRemove={() => t.setByColumn((all) => withFilter(all, f.column, null))} />
          ))}
          {columnFilters.length > 1 && <Button size="sm" variant="ghost" label="Clear all" onClick={() => t.setByColumn([])} />}
        </div>
      )}
      {asked.error && (
        <Banner
          status="error"
          title={
            <Text type="code" wordBreak="break-word" xstyle={styles.pre}>
              {asked.error}
            </Text>
          }
        />
      )}
      <RowsTable
        columns={view.columns}
        source={view.source}
        stats={writing ? null : stats}
        resetKey={view.key}
        sort={sort}
        onSort={(column) => t.setSort(nextSort(sort, column))}
        filters={t.byColumn}
        // A query of one's own says which rows it wants itself.
        onFilter={writing ? undefined : (column, f) => t.setByColumn((all) => withFilter(all, column, f))}
      />
      <div {...stylex.props(styles.foot)}>
        <Text type="supporting" color="secondary" maxLines={1} hasTabularNumbers xstyle={styles.footText}>
          {answer
            ? `${fmtNum(answer.total)}${narrowed && !writing ? ` of ${fmtNum(p.total)}` : ""} rows · ${answer.columns.length} columns`
            : `${fmtNum(p.total)} rows · ${p.columns.length} columns · ${ago(node.finished_at)}`}
          {answer && writing ? " · your query" : ""}
          {answer && sort ? ` · by ${sort.column}${sort.desc ? ", largest first" : ""}` : ""}
        </Text>
        <div {...stylex.props(styles.footEnd)}>
          {answer && (narrowed || writing || sort) ? (
            <DownloadMenu
              small
              up
              label="Download"
              title="Download the rows shown: every row of what is asked for"
              groups={[{ title: "The rows shown", items: tableItems((f) => saveQuery(answer.sql, f), answer.total) }]}
            />
          ) : (
            <DownloadMenu
              small
              up
              label="Download"
              title={`Download every row of ${shown ? `${node.name}.${shown}` : node.name}`}
              groups={[{ title: "Every row", items: tableItems((f) => saveUrl(nodeExportUrl(node.name, f, { part: shown })), p.total) }, ...zipAll]}
            />
          )}
        </div>
      </div>
    </>
  );
}

const rule = {
  borderBottomWidth: "1px",
  borderBottomStyle: "solid",
  borderBottomColor: colorVars["--color-border"],
} as const;

const styles = stylex.create({
  center: { display: "grid", placeItems: "center", flexGrow: 1, padding: spacingVars["--spacing-6"] },
  pre: { whiteSpace: "pre-wrap" },
  // What is asked of the rows, over them: a condition to filter by, or a query written out.
  ask: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    flexShrink: 0,
    paddingBlock: spacingVars["--spacing-2"],
    paddingInline: spacingVars["--spacing-4"],
    ...rule,
  },
  askSql: { flexDirection: "column", alignItems: "stretch" },
  // The box takes the room the controls beside it leave, and stays on their line.
  askInput: { flexGrow: 1, flexBasis: 0, minWidth: "120px" },
  // The condition a prompt became, on a line of its own under the box.
  askWhere: { flexBasis: "100%", minWidth: 0 },
  // The columns filtered, as tokens.
  strip: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    flexShrink: 0,
    paddingBlock: spacingVars["--spacing-2"],
    paddingInline: spacingVars["--spacing-4"],
    ...rule,
  },
  // The bar under the rows: how many, and Download.
  foot: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    flexShrink: 0,
    minHeight: "36px",
    paddingBlock: spacingVars["--spacing-1"],
    paddingInline: spacingVars["--spacing-4"],
    borderTopWidth: "1px",
    borderTopStyle: "solid",
    borderTopColor: colorVars["--color-border"],
  },
  footText: { flexGrow: 1, minWidth: 0 },
  // What can be done, at the bar's end: it keeps its width, and the text gives way.
  footEnd: { display: "flex", flexShrink: 0 },
  // The query box: an editor a few lines tall, which its corner drags taller.
  query: {
    display: "flex",
    flexDirection: "column",
    height: "96px",
    minHeight: "64px",
    maxHeight: "50vh",
    overflow: "hidden",
    resize: "vertical",
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: { default: colorVars["--color-border-emphasized"], ":focus-within": colorVars["--color-accent"] },
    borderRadius: radiusVars["--radius-element"],
  },
  queryRow: { display: "flex", flexWrap: "wrap", alignItems: "center", gap: spacingVars["--spacing-2"] },
});
