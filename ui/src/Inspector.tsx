import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { python } from "@codemirror/lang-python";
import { sql } from "@codemirror/lang-sql";
import * as stylex from "@stylexjs/stylex";
import { colorVars, radiusVars, spacingVars, textSizeVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Code } from "@astryxdesign/core/Code";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { List, ListItem } from "@astryxdesign/core/List";
import { Spinner } from "@astryxdesign/core/Spinner";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { api, type Column, type GraphNode, type RunRow } from "./api";
import { ago, dur, fmtNum, until } from "./util";
import { IconClock, IconPlay, IconSpark, IconTrash, MOD } from "./icons";
import { failed } from "./toast";
import { HistoryBrowser } from "./History";
import { useAccess } from "./Access";
import { codeGroup, DownloadMenu, outputGroups, tableItems } from "./Export";
import { nodeExportUrl, saveQuery, saveUrl } from "./download";
import { CodeEditor } from "./ui/CodeEditor";
import { chartedPart, DatasetSummary, DataTable, NodeChart, OutputPicker, useNodePreview, useOutputPart, useTableStats } from "./NodeOutput";
import { OutputChartPick, OutputRows, QUERY, useOutputTable } from "./OutputTable";
import { PAGE_ROWS } from "./rowWindow";
import { StateDot, statusOf, statusText } from "./ui/state";
import { useColorMode } from "./theme/colorMode";

type Tab = "code" | "data" | "runs" | "history";

/**
 * The pane of the selected node. From the top: what it is (its name and
 * kind) and what can be done to it (Run, download, delete); its state and
 * what it depends on; then its code, data (with its chart over it), runs and
 * history, a tab each.
 */
export function Inspector(props: {
  node: GraphNode;
  nodes: GraphNode[];
  logs: Record<string, string>;
  dark: boolean;
  onDeleted: () => void;
  onAskAI: (prompt: string) => void;
  aiEnabled: boolean;
  /** Add a package to the project and re-run this node once it is installed. */
  onInstall: (pkg: string) => Promise<void>;
  /** An install started from this node is under way. */
  installing: boolean;
  /** Changes when the node's file is picked in the side panel: show the Code tab. */
  showCode?: number;
}) {
  const { node, logs, dark, onDeleted, onAskAI, onInstall, installing } = props;
  // A viewer of a shared project reads the node but changes nothing (sharing.py).
  const { canEdit } = useAccess();
  const aiEnabled = props.aiEnabled && canEdit;
  const [tab, setTab] = useState<Tab>("code");
  const [source, setSource] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmRefresh, setConfirmRefresh] = useState(false);
  const panelId = useId();
  const dirty = source !== saved;
  const loadedFor = useRef<string>("");
  useEffect(() => {
    if (props.showCode) setTab("code");
  }, [props.showCode]);

  // Load source on selection, and again when the file changes on disk
  // (agent or external editor) — unless the user has unsaved edits.
  useEffect(() => {
    const key = `${node.name}:${node.code_hash}`;
    if (loadedFor.current.startsWith(`${node.name}:`) && dirty) return;
    if (loadedFor.current === key) return;
    loadedFor.current = key;
    api.node(node.name).then((r) => {
      setSource(r.source);
      setSaved(r.source);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node.name, node.code_hash]);

  useEffect(() => {
    setErr(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node.name]);

  const save = useCallback(async () => {
    if (!dirty || !canEdit) return;
    setErr(null);
    try {
      await api.save(node.name, source);
      setSaved(source);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [dirty, canEdit, node.name, source]);

  const run = async (fullRefresh = false) => {
    if (!canEdit) return;
    setBusy(true);
    try {
      await save();
      await api.run(node.name, true, fullRefresh);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const del = async () => {
    setConfirmDelete(false);
    try {
      await api.remove(node.name);
      onDeleted();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  // Nodes that take this one as an input: they fail once it is gone.
  const readers = props.nodes.filter((n) => n.deps.includes(node.name)).map((n) => n.name);

  const running = node.state === "running" || node.state === "queued";
  const problem = node.parse_error || (node.state === "failed" ? node.reason : null);
  // What went wrong with a save, a run or a delete shows on every tab; the node's own error with its code.
  const shownErr = err || (tab === "code" ? problem : null);

  /** The editor's shortcuts, taken before CodeMirror sees the keys (it would add a line on Ctrl+Enter). */
  const onKeys = (e: KeyboardEvent) => {
    if (!(e.ctrlKey || e.metaKey) || (e.key !== "s" && e.key !== "Enter")) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "s") save();
    else run();
  };

  return (
    <div {...stylex.props(styles.root)}>
      <header {...stylex.props(styles.head)}>
        <div {...stylex.props(styles.titleRow)}>
          <div {...stylex.props(styles.title)}>
            <Heading level={3} maxLines={1}>
              {node.name}
            </Heading>
          </div>
          <span {...stylex.props(styles.fixed)}>
            <Badge label={node.kind === "python" ? "py" : "sql"} />
          </span>
          <span {...stylex.props(styles.grow)} />
          <div {...stylex.props(styles.actions)}>
            {canEdit && running && node.last_run && (
              <Button label="Stop" onClick={() => api.cancel(node.last_run!.id).catch(failed("Couldn't stop the run"))} />
            )}
            {canEdit && (
              <Button
                variant="primary"
                label={running ? "Running…" : dirty ? "Save & run" : "Run"}
                icon={running || dirty ? undefined : <IconPlay size={11} />}
                isDisabled={busy || running || !!node.parse_error}
                onClick={() => run()}
              />
            )}
            {canEdit && node.partition_by && (
              <Button
                variant="secondary"
                label="Full refresh"
                tooltip="Rebuild the output from nothing: what the run returns becomes all of it"
                isDisabled={busy || running || !!node.parse_error}
                onClick={() => setConfirmRefresh(true)}
              />
            )}
            <DownloadMenu
              title={`Download ${node.name}: its output or its code`}
              groups={[...outputGroups(node, { title: "Output" }), codeGroup({ node: node.name })]}
            />
            {canEdit && (
              <IconButton variant="ghost" label={`Delete node ${node.name}`} tooltip="Delete node" icon={<IconTrash />} onClick={() => setConfirmDelete(true)} />
            )}
          </div>
        </div>
        <div {...stylex.props(styles.sub)}>
          <HStack gap={1.5} vAlign="center">
            <StateDot state={node.state} />
            <Text type="supporting" weight="medium" xstyle={statusText[statusOf(node.state)]}>
              {node.state}
            </Text>
            {node.reason && node.state !== "failed" && node.state !== "error" && (
              <Text type="supporting" color="secondary">
                · {node.reason}
              </Text>
            )}
          </HStack>
          {node.partition_by && (
            <Text type="supporting" color="secondary" maxLines={1}>
              partitioned by {node.partition_by}
            </Text>
          )}
          {node.key && node.key.length > 0 && (
            <Text type="supporting" color="secondary" maxLines={1}>
              key {node.key.join(", ")}
              {node.on_duplicate === "last" ? " (last row wins)" : ""}
              {node.merge ? ", runs merge by key" : ""}
            </Text>
          )}
          {node.deps.length > 0 && (
            <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.deps}>
              ← {node.deps.join(", ")}
            </Text>
          )}
          {node.schedule && (
            <HStack gap={1} vAlign="center">
              <IconClock size={12} />
              <Text type="code" size="sm" color="secondary">
                {node.schedule.cron}
              </Text>
              <Text type="supporting" color="secondary">
                {node.schedule.paused ? "(paused)" : node.schedule.next ? `(next ${until(node.schedule.next)})` : ""}
              </Text>
              {canEdit && (
                <Button
                  size="sm"
                  variant="ghost"
                  label={node.schedule.paused ? "Resume" : "Pause"}
                  onClick={() => api.pause(node.name, !node.schedule!.paused).catch(failed("Couldn't change the schedule"))}
                />
              )}
            </HStack>
          )}
          {!node.cascade && (
            <Tooltip content="Does not auto-refresh when upstream changes">
              <Text type="supporting" color="secondary">
                cascade off
              </Text>
            </Tooltip>
          )}
        </div>
        <TabList value={tab} onChange={(t) => setTab(t as Tab)} role="tablist" aria-label={`${node.name}: code, data, runs and history`} xstyle={styles.tabs}>
          <Tab
            value="code"
            label="Code"
            panelId={panelId}
            endContent={dirty ? <StatusDot variant="accent" label="Unsaved changes" tooltip="Unsaved changes" /> : undefined}
          />
          <Tab value="data" label="Data" panelId={panelId} endContent={node.rows != null ? <Badge label={fmtNum(node.rows)} /> : undefined} />
          <Tab value="runs" label="Runs" panelId={panelId} />
          <Tab value="history" label="History" panelId={panelId} />
        </TabList>
      </header>
      {shownErr && (
        <div {...stylex.props(styles.problem)}>
          <Banner
            status="error"
            title={
              <Text type="code" wordBreak="break-word" xstyle={styles.pre}>
                {shownErr}
              </Text>
            }
            isDismissable={!!err}
            onDismiss={() => setErr(null)}
            dismissLabel="Dismiss the error"
            endContent={
              !err && ((canEdit && node.missing_package) || aiEnabled) ? (
                <HStack gap={1.5}>
                  {canEdit && node.missing_package && (
                    <Button
                      size="sm"
                      label={installing ? "Installing…" : `Install ${node.missing_package.package}`}
                      isDisabled={installing}
                      tooltip={`Add ${node.missing_package.package} to the project's pyproject.toml, install it, and re-run ${node.name}`}
                      onClick={() => onInstall(node.missing_package!.package).catch((e) => setErr((e as Error).message))}
                    />
                  )}
                  {aiEnabled && (
                    <Button
                      size="sm"
                      label="Fix with AI"
                      icon={<IconSpark size={12} />}
                      onClick={() => onAskAI(`Fix \`${node.name}\` — it's failing. Read the error, fix the code, and re-run it.`)}
                    />
                  )}
                </HStack>
              ) : undefined
            }
          />
        </div>
      )}
      <div id={panelId} role="tabpanel" {...stylex.props(styles.body)}>
        {tab === "code" && (
          <div {...stylex.props(styles.fill)} onKeyDownCapture={onKeys}>
            <CodeEditor
              value={source}
              onChange={setSource}
              editable={canEdit}
              readOnly={!canEdit}
              dark={dark}
              extensions={[node.kind === "sql" ? sql() : python()]}
            />
            <div {...stylex.props(styles.foot)}>
              <Text type="code" size="sm" color="secondary" maxLines={1} xstyle={styles.footText}>
                {node.path}
              </Text>
              {canEdit ? (
                <>
                  <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.footHint}>
                    {dirty ? `unsaved · ${MOD}S save · ${MOD}Enter run` : "saved"}
                  </Text>
                  <Button size="sm" label="Save" isDisabled={!dirty} onClick={save} xstyle={styles.footButton} />
                </>
              ) : (
                <Text type="supporting" color="secondary">
                  read-only
                </Text>
              )}
            </div>
          </div>
        )}
        {tab === "data" && <DataTab node={node} dark={dark} canPrompt={aiEnabled} />}
        {tab === "runs" && <RunsTab node={node} logs={logs} />}
        {tab === "history" && (
          <HistoryBrowser
            filter={{ node: node.name }}
            tick={node.code_hash}
            dark={dark}
            layout="stack"
            restoreNote={() => (dirty ? "Your unsaved edits in the Code tab will be replaced." : null)}
            onRestored={() => {
              // Show the restored code even over unsaved edits.
              loadedFor.current = "";
              api.node(node.name).then((r) => {
                setSource(r.source);
                setSaved(r.source);
              });
            }}
          />
        )}
      </div>
      <AlertDialog
        isOpen={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete ${node.name}?`}
        description={
          `This removes ${node.path} from the project. Its code stays in the project's history, so the node can be restored.` +
          (readers.length > 0
            ? ` ${readers.length === 1 ? "One node reads" : `${readers.length} nodes read`} it and will fail without it: ${readers.join(", ")}.`
            : "")
        }
        actionLabel="Delete node"
        onAction={del}
      />
      <AlertDialog
        isOpen={confirmRefresh}
        onOpenChange={setConfirmRefresh}
        title={`Rebuild ${node.name} from nothing?`}
        description={
          `The run starts from an empty output: exuvia.partitions() is empty, and every ${node.partition_by} the run ` +
          "doesn't return is dropped from the output. Earlier runs' outputs stay readable in its history."
        }
        actionLabel="Full refresh"
        onAction={() => {
          setConfirmRefresh(false);
          run(true);
        }}
      />
    </div>
  );
}

/**
 * A node's output: every row of a table under its chart when it has one,
 * asked for as OutputTable.tsx lets them be (filtered, sorted, picked on the
 * chart, queried); or its JSON.
 */
function DataTab({ node, dark, canPrompt }: { node: GraphNode; dark: boolean; canPrompt: boolean }) {
  // A node with several outputs shows one at a time.
  const { parts, shown, pick } = useOutputPart(node);
  // The chart, over the rows, when the output shown is the one it charts;
  // what is selected on it is what the table shows.
  const hasChart = !!node.chart && !!node.artifact_id && shown === chartedPart(node);
  const multi = node.output_kind === "multi";
  const { preview: p } = useNodePreview(node, PAGE_ROWS, shown);
  const isTable = p?.kind === "table";
  // What the output's Parquet footers say: no row is read for it.
  const stats = useTableStats(node, shown, isTable);
  const t = useOutputTable(node, shown, p, { canPrompt, isTable });
  const picker = (shown !== null || (isTable && stats)) && (
    <div {...stylex.props(styles.strip)}>
      {shown !== null && <OutputPicker node={node} parts={parts} shown={shown} onPick={pick} />}
      {shown !== null &&
        (node.schema?.find((c) => c.name === shown)?.type === "table" ? (
          <Text type="supporting" color="secondary">
            SQL: <Code size="inherit">{`${node.name}.${shown}`}</Code>
          </Text>
        ) : (
          <Text type="supporting" color="secondary">
            JSON (not in SQL)
          </Text>
        ))}
      {isTable && stats && <DatasetSummary stats={stats} />}
    </div>
  );
  // A node with several outputs: all of them at once, as stored.
  const zipAll = multi
    ? [{ items: [{ key: "zip", label: "All outputs (.zip)", hint: "every output as it is stored", onPick: () => saveUrl(nodeExportUrl(node.name, "zip")) }] }]
    : [];
  if (!p)
    return (
      <div {...stylex.props(styles.center)}>
        <Spinner aria-label="Loading…" />
      </div>
    );
  if (!p.kind)
    return (
      <div {...stylex.props(styles.center)}>
        <EmptyState isCompact title="No output yet" description="Run the node." />
      </div>
    );
  if (p.kind === "table" && p.columns && p.rows)
    return (
      <div {...stylex.props(styles.fill, styles.tableFill)}>
        {picker}
        {hasChart && (
          <div {...stylex.props(styles.chart)}>
            <NodeChart node={node} dark={dark} height={200} onSelect={t.onSelect} reset={t.cleared} where={t.chartWhere} />
            <OutputChartPick t={t} />
          </div>
        )}
        <OutputRows t={t} dark={dark} stats={stats} />
      </div>
    );
  return (
    <div {...stylex.props(styles.fill)}>
      {picker}
      <pre {...stylex.props(styles.scroll, styles.mono, styles.json)}>{JSON.stringify(p.value, null, 2)}</pre>
      <div {...stylex.props(styles.foot)}>
        <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.footText}>
          JSON · {ago(node.finished_at)}
        </Text>
        <div {...stylex.props(styles.footEnd)}>
          <DownloadMenu
            small
            up
            label="Download"
            title={`Download ${shown ? `${node.name}.${shown}` : node.name}`}
            groups={[
              {
                items: [{ key: "json", label: "JSON", hint: ".json: the value as it was returned", onPick: () => saveUrl(nodeExportUrl(node.name, "json", { part: shown })) }],
              },
              ...zipAll,
            ]}
          />
        </div>
      </div>
    </div>
  );
}

function RunsTab({ node, logs }: { node: GraphNode; logs: Record<string, string> }) {
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunRow | null>(null);
  const tick = `${node.last_run?.id}:${node.last_run?.status}`;
  useEffect(() => {
    api.runs(node.name).then((r) => {
      setRuns(r);
      setSel((s) => (s && r.some((x) => x.id === s) ? s : r[0]?.id ?? null));
    });
  }, [node.name, tick]);
  useEffect(() => {
    if (sel) api.runDetail(sel).then(setDetail);
    else setDetail(null);
  }, [sel, tick]);
  if (!runs.length)
    return (
      <div {...stylex.props(styles.center)}>
        <EmptyState isCompact title="No runs yet" />
      </div>
    );
  const live = sel ? logs[sel] : undefined;
  const isLive = detail && (detail.status === "running" || detail.status === "queued");
  return (
    <div {...stylex.props(styles.fill)}>
      {/* The runs keep to the top of the pane, so the picked run's log is always in view under them. */}
      <div {...stylex.props(styles.runs)}>
        <List density="compact" hasDividers>
          {runs.map((r) => (
            <ListItem
              key={r.id}
              isSelected={r.id === sel}
              onClick={() => setSel(r.id)}
              startContent={<StateDot state={r.status} />}
              label={
                <Text maxLines={1}>
                  <Text weight="medium">{r.status}</Text>{" "}
                  <Text color="secondary">
                    · {r.trigger}
                    {r.started_by ? ` · ${r.started_by}` : ""}
                    {!r.error && r.rows != null ? ` · ${fmtNum(r.rows)} rows` : ""}
                  </Text>
                </Text>
              }
              description={
                r.error ? (
                  <Text type="supporting" maxLines={1} xstyle={statusText.error}>
                    {r.error}
                  </Text>
                ) : undefined
              }
              endContent={
                <Text type="supporting" color="secondary" hasTabularNumbers textWrap="nowrap">
                  {[dur(r.started_at, r.finished_at), ago(r.queued_at)].filter(Boolean).join(" · ")}
                </Text>
              }
            />
          ))}
        </List>
      </div>
      {detail && (
        <>
          <div {...stylex.props(styles.strip, styles.logHead)}>
            <Heading level={5} accessibilityLevel={4}>
              Log
            </Heading>
            <span {...stylex.props(styles.grow)} />
            {detail.status === "success" && detail.artifact_id && node.output_kind && node.output_kind !== "none" && (
              <DownloadMenu
                small
                label="Output of this run"
                title="Download the output this run made"
                groups={outputGroups(node, { runId: detail.id, rows: detail.rows ?? null })}
              />
            )}
          </div>
          <pre {...stylex.props(styles.scroll, styles.mono, styles.log)}>
            {(isLive ? live : detail.log) || (isLive ? "waiting for output…" : "(no output)")}
          </pre>
        </>
      )}
    </div>
  );
}

/**
 * The pane with no node selected: a query over the nodes' outputs, and the
 * runs of the whole graph, newest first.
 */
export function Overview(props: { nodes: GraphNode[]; onSelect: (n: string) => void }) {
  const [dark] = useColorMode();
  const [q, setQ] = useState("");
  const firstTable = props.nodes.find((n) => n.output_kind === "table")?.name;
  useEffect(() => {
    if (!q && firstTable) setQ(`select * from ${firstTable} limit 100`);
  }, [firstTable, q]);
  const [res, setRes] = useState<{ columns: Column[]; rows: unknown[][] } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const tick = props.nodes.map((n) => n.last_run?.id + ":" + n.last_run?.status).join();
  useEffect(() => {
    api.recentRuns().then(setRuns);
  }, [tick]);
  const exec = async () => {
    setErr(null);
    try {
      setRes(await api.query(q));
    } catch (e) {
      setErr((e as Error).message);
      setRes(null);
    }
  };
  return (
    <div {...stylex.props(styles.overview)}>
      <section {...stylex.props(styles.section)}>
        <Heading level={4} accessibilityLevel={3}>
          Query outputs
        </Heading>
        <div
          {...stylex.props(styles.query)}
          onKeyDownCapture={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
              e.preventDefault();
              e.stopPropagation();
              exec();
            }
          }}
        >
          <CodeEditor value={q} onChange={setQ} dark={dark} activeLine={false} extensions={QUERY} />
        </div>
        <div {...stylex.props(styles.queryRow)}>
          <Button size="sm" variant="primary" label="Run query" onClick={exec} />
          <DownloadMenu
            small
            align="left"
            label="Download"
            title="Download the query's whole result"
            disabled={!q.trim()}
            groups={[{ title: "Whole result", items: tableItems((f) => saveQuery(q, f)) }]}
          />
          <Text type="supporting" color="secondary">
            DuckDB · every table node is a view · {MOD}Enter
          </Text>
        </div>
        {err && (
          <Banner
            status="error"
            title={
              <Text type="code" wordBreak="break-word" xstyle={styles.pre}>
                {err}
              </Text>
            }
          />
        )}
        {res && (
          <div {...stylex.props(styles.result)}>
            <DataTable columns={res.columns} rows={res.rows} />
          </div>
        )}
      </section>
      <section {...stylex.props(styles.section)}>
        <Heading level={4} accessibilityLevel={3}>
          Recent runs
        </Heading>
        {runs.length ? (
          <div {...stylex.props(styles.recent)}>
            <List density="compact" hasDividers>
              {runs.slice(0, 25).map((r) => (
                <ListItem
                  key={r.id}
                  onClick={() => props.onSelect(r.node)}
                  startContent={<StateDot state={r.status} />}
                  label={
                    <Text maxLines={1}>
                      <Text type="code" weight="medium">
                        {r.node}
                      </Text>{" "}
                      <Text color="secondary">
                        · {r.status} · {r.trigger}
                        {r.started_by ? ` · ${r.started_by}` : ""}
                      </Text>
                    </Text>
                  }
                  endContent={
                    <Text type="supporting" color="secondary" hasTabularNumbers textWrap="nowrap">
                      {ago(r.queued_at)}
                    </Text>
                  }
                />
              ))}
            </List>
          </div>
        ) : (
          <EmptyState isCompact title="No runs yet" description="Run a node and it shows here." />
        )}
      </section>
    </div>
  );
}


const rule = {
  borderBottomWidth: "1px",
  borderBottomStyle: "solid",
  borderBottomColor: colorVars["--color-border"],
} as const;

const styles = stylex.create({
  // The pane is App's (its ground and its edge); this fills it.
  root: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: 0, minHeight: 0 },
  // The tabs sit on the head's bottom edge, so the head has no padding there.
  head: {
    display: "flex",
    flexDirection: "column",
    gap: spacingVars["--spacing-1"],
    flexShrink: 0,
    paddingBlockStart: spacingVars["--spacing-3"],
    paddingInline: spacingVars["--spacing-4"],
    ...rule,
  },
  titleRow: { display: "flex", alignItems: "center", gap: spacingVars["--spacing-2"], minWidth: 0 },
  // The name gives way, and is cut short, before the controls do.
  title: { flexShrink: 1, minWidth: 0 },
  fixed: { display: "inline-flex", flexShrink: 0 },
  actions: { display: "flex", alignItems: "center", gap: spacingVars["--spacing-1"], flexShrink: 0 },
  grow: { flexGrow: 1 },
  sub: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    columnGap: spacingVars["--spacing-3"],
    rowGap: spacingVars["--spacing-0-5"],
    minHeight: "24px",
    color: colorVars["--color-text-secondary"],
  },
  deps: { minWidth: 0 },
  // A tab's label, not its padding, starts on the title's line.
  tabs: { marginBlockStart: spacingVars["--spacing-1"], marginInlineStart: `calc(${spacingVars["--spacing-3"]} * -1)`, minWidth: 0 },
  // An error keeps to its share of the pane: a long traceback scrolls.
  problem: {
    flexShrink: 0,
    maxHeight: "40%",
    overflow: "auto",
    paddingBlockStart: spacingVars["--spacing-3"],
    paddingInline: spacingVars["--spacing-4"],
  },
  pre: { whiteSpace: "pre-wrap" },
  body: { position: "relative", display: "flex", flexDirection: "column", flexGrow: 1, minHeight: 0, overflow: "auto" },
  // A tab that lays itself out: what scrolls in it, and a bar that stays put under it.
  fill: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: 0, minHeight: 0 },
  // The Data tab of a table: where its chart leaves the rows too little room, all of it scrolls.
  tableFill: { overflowY: "auto" },
  scroll: { flexGrow: 1, minWidth: 0, minHeight: 0, overflow: "auto" },
  center: { display: "grid", placeItems: "center", flexGrow: 1, padding: spacingVars["--spacing-6"] },
  // The bar under a tab's content: what is shown, and what can be done with it.
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
  // The shortcut hint gives way after the path; the Save button never does.
  footHint: { flexShrink: 1, minWidth: 0 },
  footButton: { flexShrink: 0 },
  // What can be done, at the bar's end: it keeps its width, and the text gives way.
  footEnd: { display: "flex", flexShrink: 0 },
  // A bar over a tab's content: which output, which run.
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
  mono: {
    margin: 0,
    fontFamily: typographyVars["--font-family-code"],
    fontSize: textSizeVars["--font-size-sm"],
    lineHeight: 1.5,
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
  },
  json: { paddingBlock: spacingVars["--spacing-3"], paddingInline: spacingVars["--spacing-4"] },
  log: {
    paddingBlock: spacingVars["--spacing-3"],
    paddingInline: spacingVars["--spacing-4"],
    backgroundColor: "var(--exuvia-code-background)",
  },
  // The Data tab's chart, over its rows: as tall as it draws, the rows scroll under it.
  chart: {
    display: "flex",
    flexDirection: "column",
    gap: spacingVars["--spacing-3"],
    flexShrink: 0,
    paddingBlock: spacingVars["--spacing-3"],
    paddingInline: spacingVars["--spacing-4"],
    ...rule,
  },
  // A row's own padding and this one put its dot on the title's line.
  runs: { flexShrink: 0, maxHeight: "45%", overflow: "auto", paddingInline: spacingVars["--spacing-2"], ...rule },
  // In the overview the rows reach into the pane's padding, so their dots are on the headings' line.
  recent: { marginInline: `calc(${spacingVars["--spacing-2"]} * -1)` },
  logHead: { minHeight: "36px", paddingBlock: spacingVars["--spacing-1"] },
  overview: {
    display: "flex",
    flexDirection: "column",
    gap: spacingVars["--spacing-6"],
    flexGrow: 1,
    minWidth: 0,
    minHeight: 0,
    overflow: "auto",
    padding: spacingVars["--spacing-4"],
  },
  section: { display: "flex", flexDirection: "column", gap: spacingVars["--spacing-2"], minWidth: 0 },
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
  result: {
    maxHeight: "340px",
    overflow: "auto",
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-element"],
  },
});
