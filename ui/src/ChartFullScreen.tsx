import { useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import type { GraphNode } from "./api";
import { chartedPart, NodeChart, useNodePreview, useTableStats } from "./NodeOutput";
import { OutputChartPick, OutputRows, useOutputTable } from "./OutputTable";
import { PAGE_ROWS } from "./rowWindow";
import { fmtNum } from "./util";

/**
 * A node's chart full screen: as large as the window, and its rows under it,
 * folded away until they are asked for (read only then). The rows are the
 * Data tab's (OutputTable.tsx): filtered, sorted and queried the same way,
 * and what is selected on the chart is what they show. What is asked here
 * is its own, and goes when the chart is closed.
 */
export default function ChartFullScreen({ node, dark, onClose }: { node: GraphNode; dark: boolean; onClose: () => void }) {
  const part = chartedPart(node);
  const [rowsOpen, setRowsOpen] = useState(false);
  const { preview } = useNodePreview(node, PAGE_ROWS, part, rowsOpen);
  const stats = useTableStats(node, part, rowsOpen);
  // A chart is of a table: a selection is counted before the rows are opened.
  const t = useOutputTable(node, part, preview, { isTable: true });
  const total = preview?.kind === "table" ? preview.total : node.rows;
  return (
    <Dialog isOpen onOpenChange={(open) => !open && onClose()} variant="fullscreen">
      <Layout
        header={<DialogHeader title={node.name} subtitle={total != null ? `${fmtNum(total)} rows` : undefined} onOpenChange={() => onClose()} />}
        content={
          <LayoutContent isScrollable={false}>
            <div {...stylex.props(styles.body)}>
              <NodeChart node={node} dark={dark} height="fill" onSelect={t.onSelect} reset={t.cleared} where={t.chartWhere} canExpand={false} />
              <OutputChartPick t={t} />
              <Collapsible
                chevronPosition="start"
                isOpen={rowsOpen}
                onOpenChange={setRowsOpen}
                trigger={<Text weight="medium">{t.sel ? "The selected rows" : "The rows"}</Text>}
              >
                <div {...stylex.props(styles.rows)}>
                  <OutputRows t={t} dark={dark} stats={stats} />
                </div>
              </Collapsible>
            </div>
          </LayoutContent>
        }
      />
    </Dialog>
  );
}

const styles = stylex.create({
  // Under the header: the chart, taking what room the rows leave it.
  body: { display: "flex", flexDirection: "column", gap: spacingVars["--spacing-3"], height: "100%", minHeight: 0 },
  // The rows, opened: a pane of their own that scrolls, under the chart.
  rows: { display: "flex", flexDirection: "column", height: "45dvh", minHeight: 0 },
});
