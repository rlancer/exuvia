import type { ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Text } from "@astryxdesign/core/Text";

/**
 * The frame the code's history and a tag are read in (History.tsx,
 * Tags.tsx): a list of versions or files beside, or over, the diff of the
 * one picked.
 *
 *   side    list 300 | diff     (the main area)
 *   stack   list over diff      (a narrow pane: a node's or a view's History tab)
 *   <= 900px `side` stacks too
 *
 * The bar over the list and the bar over the diff are as tall as each other,
 * so side by side their rules meet in one line.
 */
const COMPACT = "@media (max-width: 900px)";

export const historyFrame = stylex.create({
  // It fills what it is in, a column that grows or a box with a height.
  frame: { flexGrow: 1, minWidth: 0, minHeight: 0, height: "100%" },
  alone: { display: "flex", flexDirection: "column" },
  // The rule between the list and the diff is the gap between them, in the
  // rule's colour: it is there whichever way the two are laid out.
  grid: { display: "grid", gap: "1px", backgroundColor: colorVars["--color-border"] },
  stack: { gridTemplateColumns: "minmax(0, 1fr)", gridTemplateRows: "fit-content(34%) minmax(0, 1fr)" },
  side: {
    gridTemplateColumns: { default: "300px minmax(0, 1fr)", [COMPACT]: "minmax(0, 1fr)" },
    gridTemplateRows: { default: "minmax(0, 1fr)", [COMPACT]: "fit-content(36%) minmax(0, 1fr)" },
  },
  list: { display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, backgroundColor: colorVars["--color-background-surface"] },
  diff: { display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, backgroundColor: colorVars["--color-background-surface"] },
  bar: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    flexShrink: 0,
    boxSizing: "border-box",
    minHeight: "40px",
    paddingBlock: spacingVars["--spacing-1-5"],
    paddingInline: spacingVars["--spacing-2"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  grow: { flexGrow: 1, minWidth: 0 },
  // A control in a bar keeps its words; the text beside it is what gets cut.
  fixed: { flexShrink: 0 },
  // The list (or the files) under its bar.
  scroll: { flexGrow: 1, minHeight: 0, overflowY: "auto" },
  note: { display: "grid", placeItems: "center", flexGrow: 1, minHeight: 0, padding: spacingVars["--spacing-6"] },
});

/** A line in place of a list or a diff: loading, or nothing to show. */
export function Note({ children }: { children: ReactNode }) {
  return (
    <div {...stylex.props(historyFrame.note)}>
      <Text color="secondary" justify="center">
        {children}
      </Text>
    </div>
  );
}
