import { createContext, lazy, memo, Suspense, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, durationVars, fontWeightVars, radiusVars, shadowVars, spacingVars, textSizeVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Link } from "@astryxdesign/core/Link";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { api, type GraphNode, type Preview, type Project, type ProjectView } from "./api";
import { useAccess } from "./Access";
import { chartedPart, NodeChart, OutputPicker, useNodePreview, useOutputPart, useTableStats } from "./NodeOutput";
import { OutputChartPick, OutputRows, useOutputTable } from "./OutputTable";
import { PAGE_ROWS } from "./rowWindow";
import { notebook, type Cell, type Section } from "./notebookOrder";
import { ago, fmtNum } from "./util";
import { failed } from "./toast";
import { IconChevronRight, IconCode, IconContents, IconLayers, IconPlay, IconWindow } from "./icons";
import { ContentsPanel, entryIds, sectionId, type Entry } from "./Contents";
import { oneOf, usePersisted } from "./ui/usePersisted";
import { Markdown } from "./ui/Markdown";
import { StateDot, statusOf, statusText } from "./ui/state";
import { nodeCard, StateCounts, stripeColor } from "./ui/nodeCard";

const PinnedView = lazy(() => import("./PinnedView"));

/** A pinned view's header, above the height its manifest names (as on the canvas). */
const PIN_HEAD = 38;

/** Where the contents stay open beside the notebook (when it is not this wide, they open over it, and close after a pick). */
const DOCK_PX = 600;
/** How wide the notebook is before the contents open beside it by themselves. */
const ROOMY_PX = 1000;

const openClosed = oneOf("open", "closed");

/** The notebook's scroller, which cells watch to know when they come into view. */
const Scroller = createContext<HTMLElement | null>(null);

/** Whether a model is set up to write a filter from a prompt (as in the Data tab). */
const CanPrompt = createContext(false);

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
  /** A model is set up: a cell's rows can be filtered from a prompt. */
  aiEnabled?: boolean;
}) {
  const { project, selected, onSelect } = props;
  const stages = project.stages;
  const sections = useMemo(() => notebook(project.nodes, project.views, stages ?? []), [project.nodes, project.views, stages]);
  const [scroller, setScroller] = useState<HTMLElement | null>(null);
  const cells = useRef(new Map<string, HTMLElement>());
  const first = useRef(true);
  // The node last picked in the notebook itself: its cell is under the pointer, and stays there.
  const own = useRef<string | null>(null);
  const pick = useCallback(
    (name: string | null) => {
      own.current = name;
      onSelect(name);
    },
    [onSelect],
  );
  const { onEdit } = props;
  const edit = useCallback(
    (name: string) => {
      own.current = name;
      onEdit(name);
    },
    [onEdit],
  );

  // The selected node's cell in view: centred when the notebook opens on it, brought in when it is picked elsewhere.
  useEffect(() => {
    if (!scroller) return;
    const smooth = !first.current;
    first.current = false;
    const here = own.current !== null && own.current === selected;
    own.current = null;
    const el = selected ? cells.current.get(selected) : null;
    if (!el || here) return;
    const box = scroller.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (smooth && r.top >= box.top && r.top <= box.bottom - 96) return;
    el.scrollIntoView({ block: r.height > box.height ? "start" : "center", behavior: smooth ? "smooth" : "auto" });
  }, [selected, scroller]);

  const register = (id: string) => (el: HTMLElement | null) => {
    if (el) cells.current.set(id, el);
    else cells.current.delete(id);
  };

  // The contents: beside the notebook where there is room (open by itself on a
  // wide one, else as this browser left them), over it where there is not.
  const [frame, setFrame] = useState<HTMLElement | null>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    if (!frame) return;
    setWidth(frame.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setWidth(frame.clientWidth));
    ro.observe(frame);
    return () => ro.disconnect();
  }, [frame]);
  const [kept, setKept] = usePersisted<"open" | "closed" | null>("exuvia-contents-notebook", null, openClosed);
  const [sheet, setSheet] = useState(false);
  const over = width > 0 && width < DOCK_PX;
  const contentsOpen = over ? sheet : kept ? kept === "open" : width >= ROOMY_PX;
  const setContentsOpen = useCallback((open: boolean) => (over ? setSheet(open) : setKept(open ? "open" : "closed")), [over, setKept]);
  const order = useMemo(() => entryIds(sections), [sections]);
  // An entry picked in the contents goes to the top of the page, and stays
  // there while the cells above it load and change height, until the page is
  // scrolled by hand.
  const [column, setColumn] = useState<HTMLElement | null>(null);
  const unpin = useRef<() => void>(() => {});
  useEffect(() => () => unpin.current(), []);
  const goTo = useCallback(
    (e: Entry) => {
      if (e.kind === "node") pick(e.id);
      if (over) setSheet(false);
      const el = cells.current.get(e.id);
      if (!el || !scroller) return;
      unpin.current();
      const align = () => {
        const margin = parseFloat(getComputedStyle(el).scrollMarginTop) || 0;
        scroller.scrollTop += el.getBoundingClientRect().top - scroller.getBoundingClientRect().top - margin;
      };
      align();
      const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(align);
      if (column) ro?.observe(column);
      const byHand = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
      const stop = () => {
        ro?.disconnect();
        clearTimeout(timer);
        byHand.forEach((t) => scroller.removeEventListener(t, stop));
        unpin.current = () => {};
      };
      const timer = setTimeout(stop, 2000);
      byHand.forEach((t) => scroller.addEventListener(t, stop, { passive: true }));
      unpin.current = stop;
    },
    [pick, over, scroller, column],
  );

  return (
    <Scroller.Provider value={scroller}>
    <CanPrompt.Provider value={!!props.aiEnabled}>
      <div ref={setFrame} {...stylex.props(styles.frame, contentsOpen && !over && styles.frameOpen)}>
      {contentsOpen && !over ? (
        <NotebookContents sections={sections} order={order} cells={cells} scroller={scroller} onPick={goTo} onClose={() => setContentsOpen(false)} />
      ) : (
        <div {...stylex.props(styles.rail)}>
          <IconButton
            size="sm"
            variant={contentsOpen ? "secondary" : "ghost"}
            label={contentsOpen ? "Hide the contents" : "Show the contents"}
            tooltip={contentsOpen ? "Hide the contents" : "Contents: the stages and nodes, to jump to one"}
            icon={<IconContents />}
            onClick={() => setContentsOpen(!contentsOpen)}
          />
        </div>
      )}
      {contentsOpen && over && (
        <div {...stylex.props(styles.sheet)}>
          <NotebookContents sections={sections} order={order} cells={cells} scroller={scroller} onPick={goTo} onClose={() => setContentsOpen(false)} />
        </div>
      )}
      <div ref={setScroller} {...stylex.props(styles.scroll)} onClick={(e) => e.target === e.currentTarget && pick(null)}>
        <div ref={setColumn} {...stylex.props(styles.column)}>
          <Text type="supporting" color="secondary" xstyle={styles.note}>
            The graph in reading order: each node after what it reads, chains kept together, stages as sections. Reading order is not run order: a node
            runs when what it reads changes, wherever it is on the page.
          </Text>
          {sections.map((s) => (
            <SectionBlock key={s.key} section={s} sectionRef={register(sectionId(s))}>
              {s.cells.map((c) =>
                c.kind === "node" ? (
                  <NodeCell
                    key={c.id}
                    cellRef={register(c.id)}
                    node={c.node}
                    selected={c.id === selected}
                    flash={props.flash[c.id] ?? 0}
                    dark={props.dark}
                    onSelect={pick}
                    onEdit={edit}
                  />
                ) : (
                  <ViewCell key={c.id} cellRef={register(c.id)} cell={c} project={project} dark={props.dark} onOpen={() => props.onOpenView(c.view.name)} />
                ),
              )}
            </SectionBlock>
          ))}
        </div>
      </div>
      </div>
    </CanPrompt.Provider>
    </Scroller.Provider>
  );
}

/**
 * The contents beside (or over) the notebook, marking the entry being read:
 * the last whose top has passed a line near the top of the scroller. Its own
 * component, so scrolling past a cell draws the contents again, not the cells.
 */
function NotebookContents(props: {
  sections: Section[];
  order: string[];
  cells: RefObject<Map<string, HTMLElement>>;
  scroller: HTMLElement | null;
  onPick: (e: Entry) => void;
  onClose: () => void;
}) {
  const { order, cells, scroller } = props;
  const [current, setCurrent] = useState<string | null>(null);
  useEffect(() => {
    if (!scroller) return;
    let frame = 0;
    const spy = () => {
      frame = 0;
      const box = scroller.getBoundingClientRect();
      const line = box.top + Math.min(120, box.height / 3);
      let at: string | null = null;
      for (const id of order) {
        const el = cells.current.get(id);
        if (!el) continue;
        if (el.getBoundingClientRect().top > line) break;
        at = id;
      }
      setCurrent(at ?? order[0] ?? null);
    };
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(spy);
    };
    spy();
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      scroller.removeEventListener("scroll", onScroll);
      cancelAnimationFrame(frame);
    };
  }, [scroller, order, cells]);
  return <ContentsPanel sections={props.sections} current={current} onPick={props.onPick} onClose={props.onClose} />;
}

/** A section: a stage's title, what it is for and its states, or a quiet line over nodes in no stage. */
function SectionBlock({ section, sectionRef, children }: { section: Section; sectionRef: (el: HTMLElement | null) => void; children: ReactNode }) {
  const { stage, members, continued } = section;
  return (
    <section ref={sectionRef} {...stylex.props(styles.section)}>
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
      {props.flash > 0 && <span key={props.flash} {...stylex.props(styles.ring, nodeCard.flash)} />}
      <span {...stylex.props(nodeCard.stripe, stripeColor[status])} />
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
      {busy && <div {...stylex.props(nodeCard.busyBar)} />}
      <div {...stylex.props(styles.cellBody)}>
        {node.description ? (
          // A docstring (or a SQL node's header comment) is Markdown, as a notebook's text cell is.
          <Suspense fallback={<Text xstyle={styles.prose}>{node.description}</Text>}>
            <Markdown density="compact" headingLevelStart={4} contentWidth="75ch">
              {node.description}
            </Markdown>
          </Suspense>
        ) : null}
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

/**
 * A node's output, under its chart when it has one: the Data tab's rows
 * (OutputTable.tsx), filtered, sorted and queried the same way, and what is
 * selected on the chart is what they show. Loaded when the cell comes into
 * view.
 */
function Output({ node, dark }: { node: GraphNode; dark: boolean }) {
  const { parts, shown, pick } = useOutputPart(node);
  const { canEdit } = useAccess();
  const canPrompt = useContext(CanPrompt) && canEdit;
  // A node with several outputs charts its first table: the chart shows over that one.
  const chart = !!node.chart && !!node.artifact_id && shown === chartedPart(node);
  const { preview, failed } = useNodePreview(node, PAGE_ROWS, shown);
  // A read that failed shows as no output.
  const p: Preview | null = failed ? { kind: null } : preview;
  const isTable = p?.kind === "table";
  // The quick insights, from the output's Parquet footers.
  const stats = useTableStats(node, shown, isTable);
  const t = useOutputTable(node, shown, preview, { canPrompt, isTable });

  // A click in the chart or the rows (their popovers too) is not a click on the cell.
  const chartBox = chart && (
    <div {...stylex.props(styles.output, styles.chart)} onClick={(e) => e.stopPropagation()}>
      <NodeChart node={node} dark={dark} height={220} onSelect={t.onSelect} reset={t.cleared} where={t.chartWhere} />
      <OutputChartPick t={t} />
    </div>
  );
  const picker = parts.length > 1 && shown !== null && (
    <div onClick={(e) => e.stopPropagation()}>
      <OutputPicker node={node} parts={parts} shown={shown} onPick={pick} />
    </div>
  );
  if (!p) return chartBox || <div {...stylex.props(styles.waiting)} />;
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
        {chartBox}
        <div {...stylex.props(styles.output, styles.table)} onClick={(e) => e.stopPropagation()}>
          <OutputRows t={t} dark={dark} stats={stats} />
        </div>
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
function ViewCell(props: {
  cellRef: (el: HTMLElement | null) => void;
  cell: Extract<Cell, { kind: "view" }>;
  project: Project;
  dark: boolean;
  onOpen: () => void;
}) {
  const { cell, project, dark, onOpen, cellRef } = props;
  const { view } = cell;
  const [el, setEl] = useState<HTMLElement | null>(null);
  const seen = useSeen(el);
  const size = view.canvas;
  const ref = (e: HTMLElement | null) => {
    setEl(e);
    cellRef(e);
  };
  if (cell.pinned && size)
    return (
      <div ref={ref} {...stylex.props(styles.pinned)}>
        {seen ? (
          <Suspense fallback={<div {...stylex.props(styles.pinWait, styles.height(size.height + PIN_HEAD))} />}>
            <PinnedView view={view} project={project} width={size.width} height={size.height} dark={dark} onOpen={onOpen} fill />
          </Suspense>
        ) : (
          <div {...stylex.props(styles.pinWait, styles.height(size.height + PIN_HEAD))} />
        )}
      </div>
    );
  return <ViewLine view={view} onOpen={onOpen} lineRef={cellRef} />;
}

function ViewLine({ view, onOpen, lineRef }: { view: ProjectView; onOpen: () => void; lineRef: (el: HTMLElement | null) => void }) {
  const title = view.title || view.name;
  return (
    <div ref={lineRef} {...stylex.props(styles.viewLine)}>
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

const NARROW = "@media (max-width: 1100px)";

const styles = stylex.create({
  // The contents (or their rail) beside the notebook's scroller.
  frame: {
    position: "relative",
    display: "grid",
    gridTemplateColumns: "40px minmax(0, 1fr)",
    gridTemplateRows: "minmax(0, 1fr)",
    height: "100%",
    minHeight: 0,
  },
  frameOpen: { gridTemplateColumns: "clamp(220px, 24%, 300px) minmax(0, 1fr)" },
  rail: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    paddingBlock: spacingVars["--spacing-1-5"],
    backgroundColor: colorVars["--color-background-surface"],
    borderInlineEndWidth: "1px",
    borderInlineEndStyle: "solid",
    borderInlineEndColor: colorVars["--color-border"],
  },
  // A narrow notebook: the contents open over it, beside the rail.
  sheet: {
    position: "absolute",
    insetBlock: 0,
    insetInlineStart: "40px",
    zIndex: 2,
    width: "min(300px, calc(100% - 56px))",
    boxShadow: shadowVars["--shadow-med"],
  },
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
  section: { display: "flex", flexDirection: "column", gap: spacingVars["--spacing-3"], minWidth: 0, scrollMarginBlock: spacingVars["--spacing-4"] },
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
  // Where the ring is drawn when the cell is touched (nodeCard.flash), so the cell itself is not remounted to play it again.
  ring: { position: "absolute", inset: 0, pointerEvents: "none", borderRadius: radiusVars["--radius-container"] },
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
  // The rows, with their filter over them and their count under: the rows scroll, inside the cell's room.
  table: { display: "flex", flexDirection: "column", maxHeight: "440px", overflow: "hidden" },
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

  pinned: { minWidth: 0, scrollMarginBlock: spacingVars["--spacing-4"] },
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
    scrollMarginBlock: spacingVars["--spacing-4"],
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
