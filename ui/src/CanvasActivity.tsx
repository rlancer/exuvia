import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Panel } from "@xyflow/react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, radiusVars, shadowVars, spacingVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import { IconButton } from "@astryxdesign/core/IconButton";
import { List, ListItem } from "@astryxdesign/core/List";
import { Text } from "@astryxdesign/core/Text";
import type { GraphNode } from "./api";
import { activity } from "./canvasGraph";
import { ago, until } from "./util";
import { IconActivity, IconClose } from "./icons";
import { StateDot } from "./ui/state";

/** How long a run has gone on (or waited), for "running 2m". */
function since(ts: number | null | undefined, now: number): string {
  if (!ts) return "";
  const s = Math.max(0, now - ts);
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/**
 * What the graph is doing now, over the canvas: the nodes running and
 * queued, those that failed, what the schedule runs next and what just
 * finished. A row brings its node into view (opening its stage if it is
 * folded). Closed, it is one button that counts what runs and what failed.
 */
export function ActivityPanel(props: {
  nodes: GraphNode[];
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  onFocus: (name: string) => void;
}) {
  const { nodes, isOpen, onFocus } = props;
  // Times are relative: tick while the panel shows them.
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!isOpen) return;
    setNow(Date.now() / 1000);
    const t = setInterval(() => setNow(Date.now() / 1000), 5000);
    return () => clearInterval(t);
  }, [isOpen, nodes]);
  const a = useMemo(() => activity(nodes, now, 3), [nodes, now]);
  const running = a.running.filter((n) => n.state === "running").length;

  if (!isOpen)
    return (
      <Panel position="top-right">
        <Button
          size="sm"
          label="Activity"
          icon={<IconActivity />}
          tooltip="What the graph is doing: runs, failures, what's next"
          endContent={
            a.failed.length ? <Badge variant="error" label={a.failed.length} /> : a.running.length ? <Badge variant="info" label={a.running.length} /> : undefined
          }
          onClick={() => props.onOpenChange(true)}
        />
      </Panel>
    );

  const row = (n: GraphNode, description: ReactNode) => (
    <ListItem
      key={n.name}
      onClick={() => onFocus(n.name)}
      startContent={<StateDot state={n.state} />}
      label={
        <Text weight="medium" maxLines={1} xstyle={styles.mono}>
          {n.name}
        </Text>
      }
      description={
        <Text type="supporting" color="secondary" maxLines={2}>
          {description}
        </Text>
      }
    />
  );
  const section = (title: string, rows: ReactNode[]) =>
    rows.length ? (
      <List
        density="compact"
        edge="flush"
        header={
          <Heading level={6} accessibilityLevel={3} color="secondary">
            {title}
          </Heading>
        }
      >
        {rows}
      </List>
    ) : null;
  const quiet = !a.running.length && !a.failed.length;

  return (
    <Panel position="top-right">
      <section {...stylex.props(styles.panel)} aria-label="Activity">
        <header {...stylex.props(styles.head)}>
          <IconActivity />
          <Heading level={5} accessibilityLevel={2}>
            Activity
          </Heading>
          <Text type="supporting" color="secondary" hasTabularNumbers xstyle={styles.grow}>
            {running ? `${running} running` : quiet ? "nothing running" : ""}
          </Text>
          <IconButton size="sm" variant="ghost" label="Close Activity" tooltip="Close Activity" icon={<IconClose />} onClick={() => props.onOpenChange(false)} />
        </header>
        <div {...stylex.props(styles.body)}>
          {section(
            "Running",
            a.running.map((n) =>
              row(
                n,
                n.state === "running"
                  ? `running ${since(n.last_run?.started_at, now)}${n.last_run?.trigger ? ` · ${n.last_run.trigger}` : ""}`
                  : `queued ${since(n.last_run?.queued_at, now)}`,
              ),
            ),
          )}
          {section("Failed", a.failed.map((n) => row(n, n.reason || n.parse_error || n.state)))}
          {section(
            "Next on the schedule",
            a.next.map((n) => row(n, `${until(n.schedule?.next)} · ${n.schedule?.cron}`)),
          )}
          {section(
            "Finished in the last hour",
            a.recent.map((n) => row(n, `${n.state} · ${ago(n.finished_at)}${n.rows != null ? ` · ${n.rows.toLocaleString()} rows` : ""}`)),
          )}
          {quiet && !a.next.length && !a.recent.length && (
            <Text type="supporting" color="secondary" xstyle={styles.empty}>
              Nothing has run in the last hour and nothing is scheduled.
            </Text>
          )}
        </div>
      </section>
    </Panel>
  );
}

const styles = stylex.create({
  panel: {
    display: "flex",
    flexDirection: "column",
    width: "300px",
    maxHeight: "min(520px, 60vh)",
    backgroundColor: colorVars["--color-background-surface"],
    borderWidth: "1px",
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
    boxShadow: shadowVars["--shadow-med"],
    overflow: "hidden",
  },
  head: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    paddingBlock: spacingVars["--spacing-1"],
    paddingInlineStart: spacingVars["--spacing-3"],
    paddingInlineEnd: spacingVars["--spacing-1"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  grow: { flexGrow: 1 },
  body: {
    display: "flex",
    flexDirection: "column",
    gap: spacingVars["--spacing-2"],
    overflow: "auto",
    paddingBlock: spacingVars["--spacing-2"],
    paddingInline: spacingVars["--spacing-3"],
  },
  mono: { fontFamily: typographyVars["--font-family-code"] },
  empty: { paddingBlock: spacingVars["--spacing-2"] },
});
