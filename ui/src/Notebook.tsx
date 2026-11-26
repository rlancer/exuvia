import { createContext, lazy, memo, Suspense, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, durationVars, fontWeightVars, radiusVars, spacingVars, textSizeVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { Link } from "@astryxdesign/core/Link";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { api, type GraphNode, type Preview, type Project, type ProjectView } from "./api";
import { useAccess } from "./Access";
import { DataTable, NodeChart } from "./Inspector";
import { countStates, STATE_ORDER } from "./canvasGraph";
import { notebook, type Cell, type Section } from "./notebookOrder";
import { ago, fmtNum } from "./util";
import { failed } from "./toast";
import { IconChevronRight, IconCode, IconLayers, IconPlay, IconWindow } from "./icons";
import { StateDot, statusOf, statusText } from "./ui/state";

const PinnedView = lazy(() => import("./PinnedView"));

/** Rows an output's preview shows: enough to see its shape. */
const PREVIEW_ROWS = 8;
/** A pinned view's header, above the height its manifest names (as on the canvas). */
const PIN_HEAD = 38;

/** The notebook's scroller, which cells watch to know when they come into view. */
const Scroller = createContext<HTMLElement | null>(null);

/** Whether `el` has come near the scroller's view (once it has, it stays true): what a cell waits for before it loads. */
function useSeen(el: HTMLElement | null): boolean {
  const root = useContext(Scroller);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    if (seen || !el) return;
    if (typeof IntersectionObserver === "undefined") return setSeen(true);
    const io = new IntersectionObserver((es) => es.some((e) => e.isIntersecting) && setSeen(true), { root, rootMargin: "400px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [el, root, seen]);
  return seen;
}

/**
 * The graph as a document: its nodes top to bottom in reading order
 * (notebookOrder.ts), cut into sections by stage, each node a cell with what
 * it is for, what it reads, its code and a preview of its output, and each
 * view after the last node it reads. The same graph as the canvas, and the
 * same selection: a cell picked here is the node the inspector shows.
 */
export function Notebook(props: {
  project: Project;
  selected: string | null;
  flash: Record<string, number>;
  dark: boolean;
  onSelect: (name: string | null) => void;
  onOpenView: (name: string) => void;
  /** Show the node's code in the inspector, to change it. */
  onEdit: (name: string) => void;
}) {
  const { project, selected, onSelect } = props;
  const stages = project.stages;
  const sections = useMemo(() => notebook(project.nodes, project.views, stages ?? []), [project.nodes, project.views, stages]);
  const [scroller, setScroller] = useState<HTMLElement | null>(null);
  const cells = useRef(new Map<string, HTMLElement>());
  const first = useRef(true);

  // The selected node's cell in view: centred when the notebook opens on it, brought in when it is picked elsewhere.
  useEffect(() => {
    const el = selected ? cells.current.get(selected) : null;
    if (!el || !scroller) return;
    const smooth = !first.current;
    first.current = false;
    const box = scroller.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (smooth && r.top >= box.top && r.top <= box.bottom - 96) return;
    el.scrollIntoView({ block: r.height > box.height ? "start" : "center", behavior: smooth ? "smooth" : "auto" });
  }, [selected, scroller]);

  const register = (id: string) => (el: HTMLElement | null) => {
    if (el) cells.current.set(id, el);
    else cells.current.delete(id);
  };

  return (
    <Scroller.Provider value={scroller}>
      <div ref={setScroller} {...stylex.props(styles.scroll)} onClick={(e) => e.target === e.currentTarget && onSelect(null)}>
        <div {...stylex.props(styles.column)}>
          <Text type="supporting" color="secondary" xstyle={styles.note}>
            The graph in reading order: each node after what it reads, chains kept together, stages as sections. Reading order is not run order: a node
            runs when what it reads changes, wherever it is on the page.
          </Text>
          {sections.map((s) => (
            <SectionBlock key={s.key} section={s}>
              {s.cells.map((c) =>
                c.kind === "node" ? (
                  <NodeCell
                    key={c.id}
                    cellRef={register(c.id)}
                    node={c.node}
                    selected={c.id === selected}
                    flash={props.flash[c.id] ?? 0}
                    dark={props.dark}
                    onSelect={onSelect}
                    onEdit={props.onEdit}
                  />
                ) : (
                  <ViewCell key={c.id} cell={c} project={project} dark={props.dark} onOpen={() => props.onOpenView(c.view.name)} />
                ),
              )}
            </SectionBlock>
          ))}
        </div>
      </div>
    </Scroller.Provider>
  );
}

/** A section: a stage's title, what it is for and its states, or a quiet line over nodes in no stage. */
function SectionBlock({ section, children }: { section: Section; children: ReactNode }) {
  const { stage, members, continued } = section;
  return (
    <section {...stylex.props(styles.section)}>
      {stage ? (
        <header {...stylex.props(styles.sectionHead)}>
          <HStack gap={2} vAlign="center">
            <IconLayers />
            <Heading level={3} maxLines={1}>
              {stage.title}
              {continued ? " (continued)" : ""}
            </Heading>
            <span {...stylex.props(styles.grow)} />
            <StateCounts members={members} />
          </HStack>
          {stage.description && !continued ? (
            <Text type="supporting" color="secondary">
              {stage.description}
            </Text>
          ) : null}
        </header>
      ) : section.key !== "views" ? (
        <Text type="supporting" color="secondary" xstyle={styles.loose}>
          Not in a stage
        </Text>
      ) : null}
      {children}
    </section>
  );
}

/** How many of a stage's nodes are in each state, worst first. */
function StateCounts({ members }: { members: GraphNode[] }) {
  const counts = countStates(members);
  return (
    <HStack gap={2} vAlign="center">
      {STATE_ORDER.filter((s) => counts[s]).map((s) => (
        <HStack key={s} gap={1} vAlign="center">
          <StateDot state={s} label={`${counts[s]} ${s}`} />
          <Text type="supporting" color="secondary" hasTabularNumbers>
            {counts[s]}
          </Text>
        </HStack>
      ))}
    </HStack>
  );
}

/** A node as a cell: its state and name, what it is for and reads, its code (folded), and the first rows of what it made. */
const NodeCell = memo(function NodeCell(props: {
  cellRef: (el: HTMLElement | null) => void;
  node: GraphNode;
  selected: boolean;
  flash: number;
  dark: boolean;
  onSelect: (name: string) => void;
  onEdit: (name: string) => void;
}) {
  const { node, selected, onSelect, onEdit } = props;
  const { canEdit } = useAccess();
  const [el, setEl] = useState<HTMLElement | null>(null);
  const seen = useSeen(el);
  const [code, setCode] = useState(false);
  const busy = node.state === "running" || node.state === "queued";
  const status = statusOf(node.state);
  const problem = node.parse_error || (node.state === "failed" || node.state === "error" ? node.reason : null);
  const ref = (e: HTMLElement | null) => {
    setEl(e);
    props.cellRef(e);
  };
  return (
    <article
      ref={ref}
      aria-label={node.name}
      {...stylex.props(styles.cell, selected && styles.selected)}
      onClick={() => onSelect(node.name)}
    >
      {/* A node the agent or a run just touched: a ring that fades. Keyed so it plays again, without remounting the cell. */}
      {props.flash > 0 && <span key={props.flash} {...stylex.props(styles.ring, styles.flash)} />}
      <span {...stylex.props(styles.stripe, stripe[status])} />
      <div {...stylex.props(styles.cellHead)}>
        <StateDot state={node.state} />
        <span {...stylex.props(styles.name)} title={node.name}>
          {node.name}
        </span>
        <Badge label={node.kind === "python" ? "py" : "sql"} />
        <Text type="supporting" weight="medium" xstyle={statusText[status]}>
          {node.state}
        </Text>
        <Text type="supporting" color="secondary" hasTabularNumbers maxLines={1} xstyle={styles.meta}>
          {[node.rows != null ? `${fmtNum(node.rows)} rows` : null, node.output_kind === "multi" ? `${node.schema?.length ?? 0} outputs` : null, busy ? null : ago(node.finished_at)]
            .filter(Boolean)
            .join(" · ")}
        </Text>
        <span {...stylex.props(styles.grow)} />
        {canEdit && (
          <HStack gap={1} vAlign="center">
            <Button size="sm" variant="ghost" label="Edit" icon={<IconCode />} tooltip="Change its code in the inspector" onClick={() => onEdit(node.name)} />
            <Button
              size="sm"
              label={busy ? "Running…" : "Run"}
              icon={busy ? undefined : <IconPlay size={11} />}
              isDisabled={busy || !!node.parse_error}
              tooltip="Run this node (and what is stale upstream of it)"
              onClick={() => api.run(node.name).catch(failed(`Couldn't run ${node.name}`))}
            />
          </HStack>
        )}
      </div>
      {busy && <div {...stylex.props(styles.bar)} />}
      <div {...stylex.props(styles.cellBody)}>
        {node.description ? <Text xstyle={styles.prose}>{node.description}</Text> : null}
        {node.deps.length > 0 && (
          <Text type="supporting" color="secondary">
            reads{" "}
            {node.deps.map((d, i) => (
              <span key={d}>
                {i > 0 ? ", " : ""}
                <Link type="code" size="sm" tooltip={`Go to ${d}`} onClick={(e) => {
                    e.stopPropagation();
                    onSelect(d);
                  }}>
                  {d}
                </Link>
              </span>
            ))}
          </Text>
        )}
        {problem && <Banner status="error" title={<Text type="code" wordBreak="break-word" maxLines={6} xstyle={styles.pre}>{problem}</Text>} />}
        <div>
          <Button
            size="sm"
            variant="ghost"
            label={code ? "Hide code" : "Show code"}
            icon={
              <span {...stylex.props(styles.chevron, code && styles.chevronOpen)}>
                <IconChevronRight size={12} />
              </span>
            }
            onClick={(e) => {
              e.stopPropagation();
              setCode(!code);
            }}
          />
        </div>
        {code && <Source node={node} onOpen={() => onEdit(node.name)} />}
        {seen ? <Output node={node} dark={props.dark} /> : <div {...stylex.props(styles.waiting)} />}
      </div>
    </article>
  );
});

/** A node's code, read-only and highlighted; a click opens it in the inspector's editor. */
function Source({ node, onOpen }: { node: GraphNode; onOpen: () => void }) {
  const [source, setSource] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api.node(node.name).then((r) => live && setSource(r.source), () => live && setSource(""));
    return () => {
      live = false;
    };
  }, [node.name, node.code_hash]);
  if (source === null) return <Spinner aria-label="Loading the code" />;
  return (
    <div {...stylex.props(styles.code)} onClick={onOpen} title="Open in the inspector">
      <CodeBlock code={source} language={node.kind === "sql" ? "sql" : "python"} title={node.path} size="sm" width="100%" maxHeight={360} hasLineNumbers />
    </div>
  );
}

/** The first rows of a node's output, or its chart when it has one; loaded when the cell comes into view. */
function Output({ node, dark }: { node: GraphNode; dark: boolean }) {
  const multi = node.output_kind === "multi";
  const parts = multi ? (node.schema ?? []).map((c) => c.name) : [];
  const [part, setPart] = useState<string | null>(null);
  const shown = multi ? (part !== null && parts.includes(part) ? part : (parts[0] ?? null)) : null;
  const [p, setP] = useState<Preview | null>(null);
  const chart = !!node.chart && !!node.artifact_id;
  useEffect(() => {
    if (chart) return;
    let live = true;
    api.data(node.name, PREVIEW_ROWS, shown ?? undefined).then(
      (d) => live && setP(d),
      () => live && setP({ kind: null }),
    );
    return () => {
      live = false;
    };
  }, [node.name, node.artifact_id, shown, chart]);

  if (chart)
    return (
      <div {...stylex.props(styles.output, styles.chart)}>
        <NodeChart node={node} dark={dark} height={220} />
      </div>
    );
  const picker = multi && parts.length > 1 && shown !== null && (
    <div onClick={(e) => e.stopPropagation()}>
      <SegmentedControl size="sm" value={shown} onChange={setPart} label={`${node.name}: outputs`}>
        {parts.map((name) => (
          <SegmentedControlItem key={name} value={name} label={name} />
        ))}
      </SegmentedControl>
    </div>
  );
  if (!p) return <div {...stylex.props(styles.waiting)} />;
  if (!p.kind || p.kind === "none")
    return (
      <Text type="supporting" color="secondary">
        {p.kind === "none" ? "Returns nothing." : "No output yet."}
      </Text>
    );
  if (p.kind === "table" && p.columns && p.rows)
    return (
      <>
        {picker}
        <div {...stylex.props(styles.output, styles.table)}>
          <DataTable columns={p.columns} rows={p.rows} />
        </div>
        <Text type="supporting" color="secondary" hasTabularNumbers>
          {p.rows.length < (p.total ?? 0) ? `first ${p.rows.length} of ` : ""}
          {fmtNum(p.total)} rows · {p.columns.length} columns
        </Text>
      </>
    );
  if (p.kind === "pickle")
    return (
      <Text type="supporting" color="secondary">
        A Python object, not shown.
      </Text>
    );
  return (
    <>
      {picker}
      <pre {...stylex.props(styles.output, styles.json)}>{JSON.stringify(p.value, null, 2)}</pre>
    </>
  );
}

/** A view after the last node it reads: pinned ones rendered live, the others a line that opens them. */
function ViewCell({ cell, project, dark, onOpen }: { cell: Extract<Cell, { kind: "view" }>; project: Project; dark: boolean; onOpen: () => void }) {
  const { view } = cell;
  const [el, setEl] = useState<HTMLElement | null>(null);
  const seen = useSeen(el);
  const size = view.canvas;
  if (cell.pinned && size)
    return (
      <div ref={setEl} {...stylex.props(styles.pinned)}>
        {seen ? (
          <Suspense fallback={<div {...stylex.props(styles.pinWait, styles.height(size.height + PIN_HEAD))} />}>
            <PinnedView view={view} project={project} width={size.width} height={size.height} dark={dark} onOpen={onOpen} fill />
          </Suspense>
        ) : (
          <div {...stylex.props(styles.pinWait, styles.height(size.height + PIN_HEAD))} />
        )}
      </div>
    );
  return <ViewLine view={view} onOpen={onOpen} />;
}

function ViewLine({ view, onOpen }: { view: ProjectView; onOpen: () => void }) {
  const title = view.title || view.name;
  return (
    <div {...stylex.props(styles.viewLine)}>
      <IconWindow />
      <Text weight="medium" maxLines={1}>
        {title}
      </Text>
      <StateDot state={view.state} label={`${view.state}${view.reason ? `: ${view.reason}` : ""}`} />
      <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.meta}>
        view · reads {view.reads.join(", ")}
      </Text>
      <span {...stylex.props(styles.grow)} />
      <Button size="sm" variant="ghost" label="Open" tooltip="Open in Views" onClick={onOpen} />
    </div>
  );
}

const REDUCED = "@media (prefers-reduced-motion: reduce)";
const NARROW = "@media (max-width: 1100px)";

/** Inside the cell's edge, which clips what spreads out of it. */
const flashRing = stylex.keyframes({
  "0%": { boxShadow: `inset 0 0 0 3px color-mix(in srgb, ${colorVars["--color-accent"]} 80%, transparent)` },
  "100%": { boxShadow: "inset 0 0 0 3px transparent" },
});

const slide = stylex.keyframes({
  from: { backgroundPosition: "100% 0" },
  to: { backgroundPosition: "-100% 0" },
});

const styles = stylex.create({
  scroll: { height: "100%", overflow: "auto", backgroundColor: colorVars["--color-background-body"] },
  // A reading column: about a page wide, centred.
  column: {
    display: "flex",
    flexDirection: "column",
    gap: spacingVars["--spacing-6"],
    maxWidth: "920px",
    marginInline: "auto",
    paddingBlock: { default: spacingVars["--spacing-6"], [NARROW]: spacingVars["--spacing-3"] },
    paddingInline: { default: spacingVars["--spacing-6"], [NARROW]: spacingVars["--spacing-3"] },
  },
  note: { maxWidth: "70ch" },
  grow: { flexGrow: 1 },
  section: { display: "flex", flexDirection: "column", gap: spacingVars["--spacing-3"], minWidth: 0 },
  sectionHead: {
    display: "flex",
    flexDirection: "column",
    gap: spacingVars["--spacing-1"],
    paddingBlockEnd: spacingVars["--spacing-2"],
    color: colorVars["--color-text-secondary"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  loose: { paddingBlockEnd: spacingVars["--spacing-1"] },

  cell: {
    position: "relative",
    display: "flex",
    flexDirection: "column",
    minWidth: 0,
    overflow: "hidden",
    color: colorVars["--color-text-primary"],
    backgroundColor: colorVars["--color-background-card"],
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
    scrollMarginBlock: spacingVars["--spacing-4"],
    transitionProperty: "border-color, box-shadow",
    transitionDuration: durationVars["--duration-fast"],
  },
  selected: {
    borderColor: colorVars["--color-accent"],
    boxShadow: `0 0 0 3px color-mix(in srgb, ${colorVars["--color-accent"]} 25%, transparent)`,
  },
  ring: { position: "absolute", inset: 0, pointerEvents: "none", borderRadius: radiusVars["--radius-container"] },
  flash: {
    animationName: { default: flashRing, [REDUCED]: "none" },
    animationDuration: "1.6s",
    animationTimingFunction: "ease-out",
  },
  stripe: { position: "absolute", insetBlock: 0, insetInlineStart: 0, width: "3px" },
  cellHead: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    columnGap: spacingVars["--spacing-2"],
    rowGap: spacingVars["--spacing-1"],
    minHeight: "44px",
    paddingBlock: spacingVars["--spacing-1-5"],
    paddingInlineStart: spacingVars["--spacing-4"],
    paddingInlineEnd: spacingVars["--spacing-2"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  name: {
    minWidth: 0,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    fontFamily: typographyVars["--font-family-code"],
    fontSize: textSizeVars["--font-size-base"],
    fontWeight: fontWeightVars["--font-weight-semibold"],
  },
  meta: { minWidth: 0 },
  cellBody: {
    display: "flex",
    flexDirection: "column",
    alignItems: "stretch",
    gap: spacingVars["--spacing-2"],
    minWidth: 0,
    paddingBlock: spacingVars["--spacing-3"],
    paddingInlineStart: spacingVars["--spacing-4"],
    paddingInlineEnd: spacingVars["--spacing-4"],
  },
  prose: { whiteSpace: "pre-line", maxWidth: "75ch" },
  pre: { whiteSpace: "pre-wrap" },
  chevron: { display: "inline-flex", transitionProperty: "transform", transitionDuration: durationVars["--duration-fast"] },
  chevronOpen: { transform: "rotate(90deg)" },
  code: { minWidth: 0, cursor: "pointer" },
  // Where an output's preview will be, until the cell comes into view.
  // About the height of a preview, so the page does not jump as cells above load.
  waiting: { minHeight: "280px" },
  output: {
    minWidth: 0,
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-element"],
  },
  table: { overflow: "auto", maxHeight: "320px" },
  chart: { padding: spacingVars["--spacing-3"] },
  json: {
    margin: 0,
    maxHeight: "240px",
    overflow: "auto",
    padding: spacingVars["--spacing-3"],
    fontFamily: typographyVars["--font-family-code"],
    fontSize: textSizeVars["--font-size-sm"],
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
  },
  bar: {
    height: "2px",
    backgroundImage: `linear-gradient(90deg, transparent, ${colorVars["--color-accent"]}, transparent)`,
    backgroundSize: "200% 100%",
    animationName: { default: slide, [REDUCED]: "none" },
    animationDuration: "1.2s",
    animationTimingFunction: "linear",
    animationIterationCount: "infinite",
  },

  pinned: { minWidth: 0 },
  pinWait: {
    width: "100%",
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
    backgroundColor: colorVars["--color-background-card"],
  },
  height: (height: number) => ({ height }),
  // A view is not a step of the pipeline: a dashed edge, the surface's ground (as on the canvas).
  viewLine: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    minWidth: 0,
    paddingBlock: spacingVars["--spacing-1"],
    paddingInlineStart: spacingVars["--spacing-4"],
    paddingInlineEnd: spacingVars["--spacing-2"],
    color: colorVars["--color-text-secondary"],
    backgroundColor: colorVars["--color-background-surface"],
    borderWidth: "1px",
    borderStyle: "dashed",
    borderColor: colorVars["--color-border-emphasized"],
    borderRadius: radiusVars["--radius-container"],
  },
});

/** The stripe's colour per status, as on the canvas's cards. */
const stripe = stylex.create({
  success: { backgroundColor: colorVars["--color-success"] },
  warning: { backgroundColor: colorVars["--color-warning"] },
  error: { backgroundColor: colorVars["--color-error"] },
  accent: { backgroundColor: colorVars["--color-accent"] },
  neutral: { backgroundColor: colorVars["--color-border-emphasized"] },
});
