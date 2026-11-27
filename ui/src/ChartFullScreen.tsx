import { useEffect, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import type { GraphNode } from "./api";
import {
  chartBrush,
  chartedPart,
  ChartPick,
  DataTable,
  NodeChart,
  RowsTable,
  SelectionText,
  useNodePreview,
  useRowPages,
  useSelectedRows,
  useTableStats,
  type ChartSelection,
} from "./NodeOutput";
import { PAGE_ROWS } from "./rowWindow";
import { fmtNum } from "./util";

/**
 * A node's chart full screen: as large as the window, and its rows under it,
 * folded away until they are asked for (read only then). What is selected on
 * the chart is what the rows show, as in the notebook; the selection here is
 * its own, and goes when the chart is closed.
 */
export default function ChartFullScreen({ node, dark, onClose }: { node: GraphNode; dark: boolean; onClose: () => void }) {
  const part = chartedPart(node);
  const brush = chartBrush(node.chart);
  const [sel, setSel] = useState<ChartSelection>(null);
  // Clear draws the chart again (a new `cleared`), with nothing selected.
  const [cleared, setCleared] = useState(0);
  const [rowsOpen, setRowsOpen] = useState(false);
  useEffect(() => setSel(null), [node.artifact_id]);
  const picked = useSelectedRows(node, part, sel);
  const { preview } = useNodePreview(node, PAGE_ROWS, part, rowsOpen);
  const all = useRowPages(node, part, preview);
  const stats = useTableStats(node, part, rowsOpen);
  const total = preview?.kind === "table" ? preview.total : node.rows;
  return (
    <Dialog isOpen onOpenChange={(open) => !open && onClose()} variant="fullscreen">
      <Layout
        header={<DialogHeader title={node.name} subtitle={total != null ? `${fmtNum(total)} rows` : undefined} onOpenChange={() => onClose()} />}
        content={
          <LayoutContent isScrollable={false}>
            <div {...stylex.props(styles.body)}>
              <NodeChart node={node} dark={dark} height="fill" onSelect={brush ? setSel : undefined} reset={cleared} canExpand={false} />
              {brush && (
                <ChartPick
                  kind={brush.kind}
                  selected={sel && picked ? <SelectionText sel={sel} picked={picked} /> : null}
                  onClear={() => {
                    setSel(null);
                    setCleared((n) => n + 1);
                  }}
                />
              )}
              <Collapsible
                chevronPosition="start"
                isOpen={rowsOpen}
                onOpenChange={setRowsOpen}
                trigger={<Text weight="medium">{sel && picked ? "The selected rows" : "The rows"}</Text>}
              >
                <div {...stylex.props(styles.rows)}>
                  {sel && picked ? (
                    <div {...stylex.props(styles.scroll)}>
                      <DataTable columns={picked.columns} rows={picked.rows} stats={stats} />
                    </div>
                  ) : all && preview?.kind === "table" && preview.columns ? (
                    <RowsTable columns={preview.columns} source={all} stats={stats} resetKey={`${node.artifact_id}/${part ?? ""}`} />
                  ) : (
                    <Spinner aria-label="Loading the rows" />
                  )}
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
  rows: { display: "flex", flexDirection: "column", height: "40dvh", minHeight: 0 },
  scroll: { flexGrow: 1, minHeight: 0, overflow: "auto" },
});
