import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { python } from "@codemirror/lang-python";
import { sql } from "@codemirror/lang-sql";
import embed from "vega-embed";
import { api, type Column, type GraphNode, type Preview, type RunRow } from "./api";
import { ago, dur, fmtCell, fmtNum, pressable, until } from "./util";
import { Dialog } from "./Dialog";
import { IconClock, IconPlay, IconSpark, IconTrash, MOD } from "./icons";
import { failed } from "./toast";
import { HistoryBrowser } from "./History";

/** A chart's series colours: the accent first, then the states' hues and four more, per theme. */
const SERIES = {
  dark: ["#6f9bff", "#f2b84b", "#4cc38a", "#ef6b62", "#b48cf2", "#4fc1d6", "#e88bc4", "#a0a39a"],
  light: ["#3b6fd8", "#c98a12", "#2f8f5b", "#c4413a", "#7d55cf", "#1f8ea6", "#c2518f", "#7d7b74"],
};

type Tab = "code" | "data" | "chart" | "runs" | "history";

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
}) {
  const { node, logs, dark, onDeleted, onAskAI, aiEnabled, onInstall, installing } = props;
  const [tab, setTab] = useState<Tab>("code");
  const [source, setSource] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const dirty = source !== saved;
  const loadedFor = useRef<string>("");

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
    if (tab === "chart" && !node.chart) setTab("code");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node.name]);

  const save = useCallback(async () => {
    if (!dirty) return;
    setErr(null);
    try {
      await api.save(node.name, source);
      setSaved(source);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [dirty, node.name, source]);

  const run = async () => {
    setBusy(true);
    try {
      await save();
      await api.run(node.name, true);
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

  return (
    <>
      <div className={`insp-head s-${node.state}`}>
        <div className="insp-title">
          <span className="dot" />
          <h3>{node.name}</h3>
          <button className="btn primary" onClick={run} disabled={busy || running || !!node.parse_error}>
            {running ? "Running…" : dirty ? "Save & run" : <><IconPlay size={11} /> Run</>}
          </button>
          {running && node.last_run && (
            <button className="btn" onClick={() => api.cancel(node.last_run!.id).catch(failed("Couldn't stop the run"))}>Stop</button>
          )}
          <button className="btn ghost icon-btn danger" title="Delete node" aria-label={`Delete node ${node.name}`} onClick={() => setConfirmDelete(true)}>
            <IconTrash />
          </button>
        </div>
        <div className="insp-sub">
          <span className={`kind ${node.kind}`}>{node.kind}</span>
          <span className="reason">{node.state}{node.reason && node.state !== "failed" && node.state !== "error" ? ` · ${node.reason}` : ""}</span>
          {node.deps.length > 0 && <span>← {node.deps.join(", ")}</span>}
          {node.schedule && (
            <span className="sched">
              <IconClock size={12} /> <span className="mono">{node.schedule.cron}</span>{" "}
              {node.schedule.paused ? "(paused)" : node.schedule.next ? `(next ${until(node.schedule.next)})` : ""}{" "}
              <button className="btn sm ghost" onClick={() => api.pause(node.name, !node.schedule!.paused).catch(failed("Couldn't change the schedule"))}>
                {node.schedule.paused ? "Resume" : "Pause"}
              </button>
            </span>
          )}
          {!node.cascade && <span title="Does not auto-refresh when upstream changes">cascade off</span>}
        </div>
        <div className="tabs" role="tablist" aria-label={`${node.name}: code, data, runs and history`}>
          <button role="tab" aria-selected={tab === "code"} className={`tab ${tab === "code" ? "on" : ""}`} onClick={() => setTab("code")}>
            Code{dirty ? <span title="Unsaved changes"> •</span> : null}
          </button>
          <button role="tab" aria-selected={tab === "data"} className={`tab ${tab === "data" ? "on" : ""}`} onClick={() => setTab("data")}>
            Data{node.rows != null && <span className="n">{fmtNum(node.rows)}</span>}
          </button>
          {node.chart && (
            <button role="tab" aria-selected={tab === "chart"} className={`tab ${tab === "chart" ? "on" : ""}`} onClick={() => setTab("chart")}>Chart</button>
          )}
          <button role="tab" aria-selected={tab === "runs"} className={`tab ${tab === "runs" ? "on" : ""}`} onClick={() => setTab("runs")}>Runs</button>
          <button role="tab" aria-selected={tab === "history"} className={`tab ${tab === "history" ? "on" : ""}`} onClick={() => setTab("history")} title="Every saved version of this node's code">
            History
          </button>
        </div>
      </div>
      <div className="insp-body">
        {tab === "code" && (
          <div
            className="code-wrap"
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === "s") {
                e.preventDefault();
                save();
              }
              if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                e.preventDefault();
                run();
              }
            }}
          >
            {(problem || err) && (
              <div className="err-banner">
                <div>{err || problem}</div>
                {!err && node.missing_package && (
                  <button
                    className="btn sm primary"
                    disabled={installing}
                    title={`Add ${node.missing_package.package} to the project's pyproject.toml, install it, and re-run ${node.name}`}
                    onClick={() => onInstall(node.missing_package!.package).catch((e) => setErr((e as Error).message))}
                  >
                    {installing ? "Installing…" : `Install ${node.missing_package.package}`}
                  </button>
                )}
                {aiEnabled && !err && (
                  <button className="btn sm" onClick={() => onAskAI(`Fix \`${node.name}\` — it's failing. Read the error, fix the code, and re-run it.`)}>
                    <IconSpark size={12} /> Fix with AI
                  </button>
                )}
              </div>
            )}
            <CodeMirror
              value={source}
              onChange={setSource}
              theme={dark ? "dark" : "light"}
              extensions={[node.kind === "sql" ? sql() : python(), EditorView.lineWrapping]}
              basicSetup={{ foldGutter: false, highlightActiveLine: true }}
              style={{ flex: 1, minHeight: 0, overflow: "auto" }}
              height="100%"
            />
            <div className="code-bar">
              <span className="mono">{node.path}</span>
              <span className="spacer" />
              <span>{dirty ? `unsaved · ${MOD}S save · ${MOD}Enter run` : "saved"}</span>
              <button className="btn sm" onClick={save} disabled={!dirty}>Save</button>
            </div>
          </div>
        )}
        {tab === "data" && <DataTab node={node} />}
        {tab === "chart" && node.chart && <ChartTab node={node} dark={dark} />}
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
      {confirmDelete && (
        <Dialog title={`Delete ${node.name}?`} onClose={() => setConfirmDelete(false)}>
          <p className="hub-muted">
            This removes <span className="mono">{node.path}</span> from the project. Its code stays in the project's
            history, so the node can be restored.
          </p>
          {readers.length > 0 && (
            <p className="hub-muted">
              {readers.length === 1 ? "One node reads" : `${readers.length} nodes read`} it and will fail without it:{" "}
              <span className="mono">{readers.join(", ")}</span>.
            </p>
          )}
          <div className="actions">
            <button type="button" className="btn ghost" onClick={() => setConfirmDelete(false)}>Cancel</button>
            <button type="button" className="btn destroy" onClick={del}>Delete node</button>
          </div>
        </Dialog>
      )}
    </>
  );
}

export function DataTable({ columns, rows }: { columns: Column[]; rows: unknown[][] }) {
  const numeric = useMemo(
    () => columns.map((c) => /int|float|double|decimal|numeric|bigint|real/i.test(c.type)),
    [columns],
  );
  return (
    <table className="grid">
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c.name}>
              {c.name}
              <span className="t">{c.type.toLowerCase()}</span>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            {r.map((v, j) => (
              <td key={j} className={numeric[j] ? "num" : ""} title={typeof v === "string" ? v : undefined}>
                {fmtCell(v)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DataTab({ node }: { node: GraphNode }) {
  const [p, setP] = useState<Preview | null>(null);
  useEffect(() => {
    let live = true;
    api.data(node.name, 300).then((d) => live && setP(d));
    return () => {
      live = false;
    };
  }, [node.name, node.artifact_id]);
  if (!p) return <div className="placeholder">Loading…</div>;
  if (!p.kind) return <div className="placeholder">No output yet — run the node.</div>;
  if (p.kind === "table" && p.columns && p.rows)
    return (
      <>
        <DataTable columns={p.columns} rows={p.rows} />
        <div className="data-foot">
          {p.rows.length < (p.total ?? 0) ? `first ${p.rows.length} of ` : ""}
          {fmtNum(p.total)} rows · {p.columns.length} columns · {ago(node.finished_at)}
        </div>
      </>
    );
  return <pre className="json">{JSON.stringify(p.value, null, 2)}</pre>;
}

function ChartTab({ node, dark }: { node: GraphNode; dark: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    let finalize: (() => void) | undefined;
    api.data(node.name, 5000).then(async (d) => {
      if (cancelled || !ref.current) return;
      if (d.kind !== "table" || !d.columns || !d.rows) {
        setError("Charts need a table output. Run the node first.");
        return;
      }
      const cols = d.columns.map((c) => c.name);
      const values = d.rows.map((r) => Object.fromEntries(r.map((v, i) => [cols[i], v])));
      const css = getComputedStyle(document.documentElement);
      const v = (k: string) => css.getPropertyValue(k).trim();
      const spec = {
        $schema: "https://vega.github.io/schema/vega-lite/v5.json",
        width: "container",
        height: 300,
        autosize: { type: "fit", contains: "padding" },
        ...node.chart,
        data: { values },
        config: {
          background: "transparent",
          font: v("--sans") || "sans-serif",
          view: { stroke: null },
          axis: { labelColor: v("--muted"), titleColor: v("--muted"), gridColor: v("--line"), domainColor: v("--line-strong"), tickColor: v("--line-strong") },
          legend: { labelColor: v("--muted"), titleColor: v("--muted") },
          range: { category: dark ? SERIES.dark : SERIES.light },
          ...(node.chart?.config as object | undefined),
        },
      };
      try {
        setError(null);
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const res = await embed(ref.current, spec as any, { actions: { export: true, source: false, compiled: false, editor: false } });
        finalize = () => res.finalize();
      } catch (e) {
        setError(`Chart spec error: ${(e as Error).message}`);
      }
    });
    return () => {
      cancelled = true;
      finalize?.();
    };
  }, [node.name, node.artifact_id, node.chart, dark]);
  return (
    <div className="chart-wrap">
      {error && <div className="err-banner"><div>{error}</div></div>}
      <div ref={ref} />
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
  if (!runs.length) return <div className="placeholder">No runs yet.</div>;
  const live = sel ? logs[sel] : undefined;
  const isLive = detail && (detail.status === "running" || detail.status === "queued");
  return (
    <>
      <ul className="runs">
        {runs.map((r) => (
          <li key={r.id} className={`s-${r.status} ${r.id === sel ? "on" : ""}`} {...pressable(() => setSel(r.id))} aria-pressed={r.id === sel}>
            <span className="dot" />
            <span>{r.status}</span>
            <span className="trig">
              {r.trigger}
              {r.started_by ? ` · ${r.started_by}` : ""}
              {r.error ? <span className="err"> · {r.error}</span> : r.rows != null ? ` · ${fmtNum(r.rows)} rows` : ""}
            </span>
            <span className="when">{dur(r.started_at, r.finished_at) || ago(r.queued_at)} · {ago(r.queued_at)}</span>
          </li>
        ))}
      </ul>
      {detail && <pre className="log">{(isLive ? live : detail.log) || (isLive ? "waiting for output…" : "(no output)")}</pre>}
    </>
  );
}

export function Overview(props: { nodes: GraphNode[]; onSelect: (n: string) => void }) {
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
    <div className="overview">
      <div className="sql-box">
        <h4>Query outputs</h4>
        <textarea
          value={q}
          aria-label="SQL over the nodes' outputs"
          spellCheck={false}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") exec();
          }}
        />
        <div className="row">
          <button className="btn sm primary" onClick={exec}>Run query</button>
          <span>DuckDB · every table node is a view · {MOD}Enter</span>
        </div>
        {err && <div className="err-banner"><div>{err}</div></div>}
        {res && (
          <div className="sql-result">
            <DataTable columns={res.columns} rows={res.rows} />
          </div>
        )}
      </div>
      <div>
        <h4>Recent runs</h4>
        <ul className="runs boxed">
          {runs.slice(0, 25).map((r) => (
            <li key={r.id} className={`s-${r.status}`} {...pressable(() => props.onSelect(r.node))} title={`Open ${r.node}`}>
              <span className="dot" />
              <span>{r.status}</span>
              <span className="trig"><span className="mono">{r.node}</span> · {r.trigger}{r.started_by ? ` · ${r.started_by}` : ""}</span>
              <span className="when">{ago(r.queued_at)}</span>
            </li>
          ))}
          {!runs.length && <li className="none">No runs yet. Run a node and it shows here.</li>}
        </ul>
      </div>
    </div>
  );
}
