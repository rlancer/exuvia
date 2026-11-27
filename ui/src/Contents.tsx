import { useEffect, useMemo, useRef } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, radiusVars, shadowVars, spacingVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Heading } from "@astryxdesign/core/Heading";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Text } from "@astryxdesign/core/Text";
import { TreeList, type TreeListItemData } from "@astryxdesign/core/TreeList";
import type { Cell, Section } from "./notebookOrder";
import { IconChevronLeft, IconLayers, IconWindow } from "./icons";
import { StateDot } from "./ui/state";

/** What an entry of the contents stands for: a stage's section, or one of its cells. */
export type Entry = { kind: "section"; id: string; section: Section } | Cell;

/** The id of a section's entry (a cell's is its own `id`). */
export const sectionId = (s: Section) => `section:${s.key}`;

/** The entries in reading order, as the notebook shows them: the ids a scroll-spy walks. */
export function entryIds(sections: Section[], views = true): string[] {
  return sections.flatMap((s) => [...(s.stage ? [sectionId(s)] : []), ...s.cells.filter((c) => views || c.kind === "node").map((c) => c.id)]);
}

/**
 * The tree's rows: a stage is a branch over its cells, open to start with;
 * cells in no stage sit at the top level where they come.
 */
function rows(sections: Section[], opts: { views: boolean; current: string | null; onPick: (e: Entry) => void }): TreeListItemData[] {
  const leaf = (c: Cell): TreeListItemData =>
    c.kind === "node"
      ? {
          id: c.id,
          label: (
            <Text maxLines={1} xstyle={styles.mono}>
              {c.node.name}
            </Text>
          ),
          startContent: <StateDot state={c.node.state} />,
          isSelected: opts.current === c.id,
          onClick: () => opts.onPick(c),
        }
      : {
          id: c.id,
          label: (
            <Text maxLines={1} color="secondary">
              {c.view.title || c.view.name}
            </Text>
          ),
          startContent: <IconWindow />,
          isSelected: opts.current === c.id,
          onClick: () => opts.onPick(c),
        };
  return sections.flatMap((s): TreeListItemData[] => {
    const cells = s.cells.filter((c) => opts.views || c.kind === "node").map(leaf);
    if (!s.stage) return cells;
    const id = sectionId(s);
    const nodes = s.cells.filter((c) => c.kind === "node").length;
    return [
      {
        id,
        label: (
          <Text weight="medium" maxLines={1}>
            {s.stage.title}
            {s.continued ? " (continued)" : ""}
          </Text>
        ),
        startContent: <IconLayers />,
        endContent: (
          <Text type="supporting" color="secondary" hasTabularNumbers>
            {nodes}
          </Text>
        ),
        isSelected: opts.current === id,
        isExpanded: true,
        onClick: () => opts.onPick({ kind: "section", id, section: s }),
        children: cells,
      },
    ];
  });
}

/**
 * The graph's table of contents: its stages, each foldable, over their nodes
 * (and, in the notebook, the views after them), in the notebook's reading
 * order. The entry for where you are is marked and kept in view; a click goes
 * there. `floating` draws it as a panel over the canvas, else as a pane.
 */
export function ContentsPanel(props: {
  sections: Section[];
  current: string | null;
  onPick: (e: Entry) => void;
  onClose: () => void;
  views?: boolean;
  floating?: boolean;
}) {
  const { sections, current, onPick, views = true } = props;
  const items = useMemo(() => rows(sections, { views, current, onPick }), [sections, views, current, onPick]);
  const nodes = useMemo(() => sections.reduce((n, s) => n + s.cells.filter((c) => c.kind === "node").length, 0), [sections]);
  const body = useRef<HTMLDivElement>(null);
  // Keep the marked entry in view as the page under it scrolls.
  useEffect(() => {
    const row = current ? body.current?.querySelector('[aria-selected="true"]') : null;
    row?.scrollIntoView({ block: "nearest" });
  }, [current]);
  return (
    <section aria-label="Contents" {...stylex.props(styles.panel, props.floating ? styles.floating : styles.pane)}>
      <header {...stylex.props(styles.head)}>
        <Heading level={5} accessibilityLevel={2}>
          Contents
        </Heading>
        <Text type="supporting" color="secondary" hasTabularNumbers xstyle={styles.grow}>
          {nodes} {nodes === 1 ? "node" : "nodes"}
        </Text>
        <IconButton size="sm" variant="ghost" label="Hide the contents" tooltip="Hide the contents" icon={<IconChevronLeft />} onClick={props.onClose} />
      </header>
      <div ref={body} {...stylex.props(styles.body)}>
        <TreeList items={items} density="compact" variant="noGuides" aria-label="The graph's contents" />
      </div>
    </section>
  );
}

const styles = stylex.create({
  panel: {
    display: "flex",
    flexDirection: "column",
    minHeight: 0,
    overflow: "hidden",
    backgroundColor: colorVars["--color-background-surface"],
  },
  // Beside the notebook: its full height, a rule between them.
  pane: {
    height: "100%",
    borderInlineEndWidth: "1px",
    borderInlineEndStyle: "solid",
    borderInlineEndColor: colorVars["--color-border"],
  },
  // Over the canvas, as the Activity panel is.
  floating: {
    width: "260px",
    maxHeight: "min(560px, 60vh)",
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
    boxShadow: shadowVars["--shadow-med"],
  },
  head: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    minHeight: "40px",
    paddingBlock: spacingVars["--spacing-1"],
    paddingInlineStart: spacingVars["--spacing-3"],
    paddingInlineEnd: spacingVars["--spacing-1"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  grow: { flexGrow: 1 },
  body: {
    flexGrow: 1,
    minHeight: 0,
    overflow: "auto",
    paddingBlock: spacingVars["--spacing-1"],
    paddingInline: spacingVars["--spacing-1"],
  },
  mono: { fontFamily: typographyVars["--font-family-code"] },
});
