import { useEffect, useMemo, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Link } from "@astryxdesign/core/Link";
import { pixel, proportional, Table, type TableColumn } from "@astryxdesign/core/Table";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { VStack } from "@astryxdesign/core/VStack";
import { api, type LiveRow, type ScheduleRow, type ScheduleState } from "./api";
import { useAccess } from "./Access";
import { IconClock, IconClose } from "./icons";
import { failed } from "./toast";
import { StateDot } from "./ui/state";
import { ago, until } from "./util";

/** How many fires the Upcoming list shows, across every node. */
const UPCOMING = 12;

/**
 * A fire time on the project's clock: "Mon 18:00" within the coming week,
 * "Dec 3 18:00" after it. A timezone the browser doesn't know falls back to
 * the browser's own.
 */
export function fireTime(ts: number, timeZone: string | null, now = Date.now() / 1000): string {
  const soon = ts - now < 6 * 86400;
  const opts: Intl.DateTimeFormatOptions = soon
    ? { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }
    : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  try {
    return new Intl.DateTimeFormat("en-US", { ...opts, timeZone: timeZone ?? undefined }).format(ts * 1000).replace(",", "");
  } catch {
    return new Intl.DateTimeFormat("en-US", opts).format(ts * 1000).replace(",", "");
  }
}

/** Every node's next fires, merged and soonest first. */
export function upcoming(rows: ScheduleRow[], limit = UPCOMING): { at: number; node: string; cron: string }[] {
  return rows
    .flatMap((r) => r.upcoming.map((at) => ({ at, node: r.name, cron: r.cron })))
    .sort((a, b) => a.at - b.at || a.node.localeCompare(b.node))
    .slice(0, limit);
}

/** "every open", "at most every 5m": how often a live node re-runs. */
export function liveEvery(seconds: number): string {
  if (!seconds) return "every open";
  const unit = seconds % 3600 === 0 ? [3600, "h"] : seconds % 60 === 0 ? [60, "m"] : [1, "s"];
  return `when over ${seconds / (unit[0] as number)}${unit[1]} old`;
}

/**
 * Schedule mode: when the graph runs by itself. Every node with a cron,
 * soonest first, with its next fires, its last scheduled run and what a new
 * output from it cascades to; the next fires across the project; and the
 * live nodes, which run when a view opens rather than on a clock.
 */
export function Schedule(props: {
  /** Changes on every graph or run event: read the schedule again. */
  tick: unknown;
  onOpenNode: (name: string) => void;
  /** Back to the graph: the page is opened from the side panel's Settings tab. */
  onClose: () => void;
}) {
  const { canEdit } = useAccess();
  const [sched, setSched] = useState<ScheduleState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Relative times ("in 4m") move on without an event.
  const [, setNow] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setNow((n) => n + 1), 30_000);
    return () => window.clearInterval(t);
  }, []);
  useEffect(() => {
    let on = true;
    api.schedule().then(
      (s) => on && (setSched(s), setErr(null)),
      (e: Error) => on && setErr(e.message),
    );
    return () => {
      on = false;
    };
  }, [props.tick]);

  const tz = sched?.timezone ?? null;
  const clock = tz ? `${tz} time` : "this server's local time";
  const next = useMemo(() => upcoming(sched?.nodes ?? []), [sched]);

  const open = (name: string) => (
    <Link onClick={() => props.onOpenNode(name)} tooltip="Open it on the graph">
      {name}
    </Link>
  );
  const stateOf = (state: string | null) => <StateDot state={state} />;

  const columns: TableColumn<ScheduleRow>[] = [
    {
      key: "name",
      header: "Node",
      width: proportional(1.4, { minWidth: 140 }),
      renderCell: (r) => (
        <HStack gap={1.5} vAlign="center">
          {stateOf(r.state)}
          {open(r.name)}
        </HStack>
      ),
    },
    {
      key: "cron",
      header: "Schedule",
      width: proportional(1.2, { minWidth: 130 }),
      renderCell: (r) => (
        <VStack gap={0.5}>
          <Text type="code" size="sm">
            {r.cron}
          </Text>
          {r.source === "config" && (
            <Tooltip content="Set from the workbench or the API, in exuvia.json; it wins over the node's code">
              <Text type="supporting" color="secondary">
                from exuvia.json
              </Text>
            </Tooltip>
          )}
        </VStack>
      ),
    },
    {
      key: "next",
      header: "Next run",
      width: proportional(1.3, { minWidth: 140 }),
      renderCell: (r) =>
        r.parse_error ? (
          <Text type="supporting" color="secondary">
            not while its code is broken
          </Text>
        ) : r.paused ? (
          <Text type="supporting" color="secondary">
            paused
          </Text>
        ) : r.next ? (
          <Tooltip content={`Then ${r.upcoming.slice(1).map((t) => fireTime(t, tz)).join(", ")} (${clock})`}>
            <VStack gap={0.5}>
              <Text hasTabularNumbers>{fireTime(r.next, tz)}</Text>
              <Text type="supporting" color="secondary">
                {until(r.next)}
              </Text>
            </VStack>
          </Tooltip>
        ) : null,
    },
    {
      key: "last",
      header: "Last scheduled run",
      width: proportional(1.3, { minWidth: 140 }),
      renderCell: (r) => <LastRun run={r.last} none="not yet" />,
    },
    {
      key: "cascades",
      header: "Then runs",
      width: proportional(1.5, { minWidth: 120 }),
      renderCell: (r) =>
        r.cascades.length ? (
          <Tooltip content={`A new output re-runs ${r.cascades.join(", ")}`}>
            <Text type="supporting" color="secondary" maxLines={2}>
              {r.cascades.join(", ")}
            </Text>
          </Tooltip>
        ) : (
          <Text type="supporting" color="secondary">
            —
          </Text>
        ),
    },
    ...(canEdit
      ? [
          {
            key: "paused",
            header: "",
            width: pixel(96),
            align: "end",
            renderCell: (r: ScheduleRow) => (
              <Button
                size="sm"
                variant="ghost"
                label={r.paused ? "Resume" : "Pause"}
                tooltip={r.paused ? `Run ${r.name} on its schedule again` : `Stop running ${r.name} on its schedule, until resumed`}
                clickAction={() => api.pause(r.name, !r.paused).then(() => undefined, failed("Couldn't change the schedule"))}
              />
            ),
          } satisfies TableColumn<ScheduleRow>,
        ]
      : []),
  ];

  const liveColumns: TableColumn<LiveRow>[] = [
    {
      key: "name",
      header: "Node",
      width: proportional(1.4, { minWidth: 140 }),
      renderCell: (r) => (
        <HStack gap={1.5} vAlign="center">
          {stateOf(r.state)}
          {open(r.name)}
        </HStack>
      ),
    },
    {
      key: "views",
      header: "Re-runs when one opens",
      width: proportional(2, { minWidth: 160 }),
      renderCell: (r) => (
        <Text type="supporting" color="secondary" maxLines={2}>
          {r.views.length ? r.views.join(", ") : "no view reads it yet"}
        </Text>
      ),
    },
    {
      key: "live",
      header: "How often",
      width: proportional(1.2, { minWidth: 120 }),
      renderCell: (r) => (
        <Text type="supporting" color="secondary">
          {liveEvery(r.live)}
        </Text>
      ),
    },
    {
      key: "last",
      header: "Last refresh",
      width: proportional(1.3, { minWidth: 140 }),
      renderCell: (r) => <LastRun run={r.last} none="not yet" />,
    },
  ];

  const nothing = sched && sched.nodes.length === 0 && sched.live.length === 0;

  return (
    <section {...stylex.props(styles.page)}>
      <div {...stylex.props(styles.head)}>
        <IconClock />
        <Heading level={4} accessibilityLevel={2}>
          Schedule
        </Heading>
        <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.hint}>
          When the graph runs by itself. Times are {clock}.
        </Text>
        <span {...stylex.props(styles.grow)} />
        <IconButton variant="ghost" size="sm" label="Close the schedule" tooltip="Close and go back to the graph" icon={<IconClose />} onClick={props.onClose} />
      </div>
      <div {...stylex.props(styles.body)}>
        {err && <Banner status="error" title={`Couldn't read the schedule: ${err}`} />}
        {sched?.scheduler === "external" && (
          <Banner
            status="info"
            title="An outside scheduler runs these"
            description="EXUVIA_SCHEDULER=external: the times below are when it is due to call /api/schedule/tick."
          />
        )}
        {nothing ? (
          <EmptyState
            icon={<IconClock size={32} />}
            title="Nothing runs by itself yet"
            description={
              'Give a node a cron to run it on a clock: schedule="0 18 * * 1-5" in @exuvia.node, or a "-- schedule:" line in SQL. ' +
              "A node marked live=True runs when a view that reads it opens."
            }
          />
        ) : (
          sched && (
            <>
              {sched.nodes.length > 0 && (
                <VStack gap={2}>
                  <Heading level={5} accessibilityLevel={3}>
                    On a schedule
                  </Heading>
                  <Table<ScheduleRow> data={sched.nodes} columns={columns} idKey="name" density="compact" />
                </VStack>
              )}
              {next.length > 0 && (
                <VStack gap={2}>
                  <Heading level={5} accessibilityLevel={3}>
                    Coming up
                  </Heading>
                  <div {...stylex.props(styles.timeline)}>
                    {next.map((f) => (
                      <HStack key={`${f.node}@${f.at}`} gap={3} vAlign="center" xstyle={styles.fire}>
                        <Text hasTabularNumbers xstyle={styles.when}>
                          {fireTime(f.at, tz)}
                        </Text>
                        <Text type="supporting" color="secondary" xstyle={styles.until}>
                          {until(f.at)}
                        </Text>
                        {open(f.node)}
                      </HStack>
                    ))}
                  </div>
                </VStack>
              )}
              {sched.live.length > 0 && (
                <VStack gap={2}>
                  <Heading level={5} accessibilityLevel={3}>
                    Live: when a view opens
                  </Heading>
                  <Table<LiveRow> data={sched.live} columns={liveColumns} idKey="name" density="compact" />
                </VStack>
              )}
            </>
          )
        )}
      </div>
    </section>
  );
}

function LastRun(props: { run: ScheduleRow["last"]; none: string }) {
  const r = props.run;
  if (!r)
    return (
      <Text type="supporting" color="secondary">
        {props.none}
      </Text>
    );
  const at = r.finished_at ?? r.started_at ?? r.queued_at;
  const line = (
    <HStack gap={1.5} vAlign="center">
      <StateDot state={r.status} />
      <Text type="supporting" color="secondary">
        {r.status} · {ago(at)}
      </Text>
    </HStack>
  );
  return r.error ? <Tooltip content={r.error}>{line}</Tooltip> : line;
}

const styles = stylex.create({
  page: { display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, backgroundColor: colorVars["--color-background-surface"] },
  head: {
    display: "flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    paddingBlock: spacingVars["--spacing-2"],
    paddingInline: spacingVars["--spacing-4"],
    color: colorVars["--color-text-secondary"],
    borderBottomWidth: "1px",
    borderBottomStyle: "solid",
    borderBottomColor: colorVars["--color-border"],
  },
  hint: { display: { default: "block", "@media (max-width: 1100px)": "none" } },
  grow: { flexGrow: 1 },
  body: {
    display: "flex",
    flexDirection: "column",
    gap: spacingVars["--spacing-6"],
    padding: spacingVars["--spacing-4"],
    overflowY: "auto",
    minHeight: 0,
    flexGrow: 1,
  },
  timeline: { display: "flex", flexDirection: "column", gap: spacingVars["--spacing-1"] },
  fire: { minWidth: 0 },
  when: { flexShrink: 0, minWidth: "96px" },
  until: { flexShrink: 0, minWidth: "64px" },
});
