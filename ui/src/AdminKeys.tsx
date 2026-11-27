import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { MetadataList, MetadataListItem } from "@astryxdesign/core/MetadataList";
import { Spinner } from "@astryxdesign/core/Spinner";
import { pixel, proportional, Table, useTableSortable, useTableSortableState, type TableColumn } from "@astryxdesign/core/Table";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { VStack } from "@astryxdesign/core/VStack";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import { dataKeysApi, type DataKeysUsage, type KeyUsage } from "./api";
import { notifyError } from "./toast";
import { IconTrash } from "./icons";
import { ago, fmtNum } from "./util";

/** Requests, queries and failures, however long a span. */
type Counted = Pick<KeyUsage, "requests" | "queries" | "errors">;

/** How many days of use the page counts. */
const DAYS = 30;

/** "12 requests, 3 queries, 1 failed": a key's use in words; "not used" when it wasn't. */
export function usageLine(u: Counted): string {
  if (!u.requests) return "not used";
  const parts = [`${fmtNum(u.requests)} request${u.requests === 1 ? "" : "s"}`];
  if (u.queries) parts.push(`${fmtNum(u.queries)} quer${u.queries === 1 ? "y" : "ies"}`);
  if (u.errors) parts.push(`${fmtNum(u.errors)} failed`);
  return parts.join(", ");
}

/** The share of requests that failed, as "4%" ("<1%" when some did but under one in a hundred); "" when none did. */
export function failedShare(u: Counted): string {
  if (!u.requests || !u.errors) return "";
  const pct = (u.errors / u.requests) * 100;
  return pct < 1 ? "<1%" : `${Math.round(pct)}%`;
}

/** "Nov 27": a day as usage counts it (UTC), in the reader's words. */
export const dayLabel = (day: string) =>
  new Date(`${day}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });

/** A key as a table row: flat, so the table sorts on any column. */
type KeyRow = {
  id: string;
  name: string;
  hint: string;
  projects: string;
  owner: string;
  ownerDisabled: boolean;
  today: number;
  week: number;
  month: number;
  usage: KeyUsage;
  last_used: number;
  created_at: number;
};

/**
 * The admin page's API keys section: every data API key on the hub (exuvia/datakeys.py), whose it is,
 * what it reads and how much it is used (requests today, this week and over 30 days, its queries and
 * failures), with the requests turned away for an unknown or revoked key. An admin can revoke any key.
 */
export function ApiKeysPanel(props: { people: boolean; self: string | null }) {
  const [data, setData] = useState<DataKeysUsage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [revoking, setRevoking] = useState<KeyRow | null>(null);
  const narrow = useMediaQuery("(max-width: 800px)");
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const load = useCallback(() => {
    setBusy(true);
    dataKeysApi.usage(DAYS).then(
      (u) => live.current && (setData(u), setErr(null), setBusy(false)),
      (e: Error) => live.current && (setErr(e.message), setBusy(false)),
    );
  }, []);
  useEffect(() => load(), [load]);

  const rows: KeyRow[] = useMemo(
    () =>
      (data?.keys ?? []).map((k) => ({
        id: k.id,
        name: k.name,
        hint: k.hint,
        projects: k.projects.join(", "),
        owner: k.owner ? (k.owner.id === props.self ? `${k.owner.name} (you)` : k.owner.name) : "the operator",
        ownerDisabled: !!k.owner?.disabled,
        today: k.usage.today,
        week: k.usage.week,
        month: k.usage.requests,
        usage: k.usage,
        last_used: k.last_used ?? 0,
        created_at: k.created_at,
      })),
    [data, props.self],
  );
  const { sortedData, sortConfig } = useTableSortableState<KeyRow>({
    data: rows,
    defaultSort: [{ sortKey: "month", direction: "descending" }],
  });
  const sortPlugin = useTableSortable<KeyRow>(sortConfig);

  const revoke = async (k: KeyRow) => {
    try {
      await dataKeysApi.revoke(k.id);
      load();
    } catch (e) {
      notifyError(`Couldn't revoke ${k.name}`, e);
    }
  };

  if (err && !data)
    return <Banner role="alert" status="error" title="Couldn't load the keys" description={err} endContent={<Button size="sm" label="Try again" onClick={load} />} />;
  if (!data)
    return (
      <Center padding={6}>
        <Spinner label="Loading…" />
      </Center>
    );

  const total = data.daily.reduce(
    (a, d) => ({ requests: a.requests + d.requests, queries: a.queries + d.queries, errors: a.errors + d.errors }),
    { requests: 0, queries: 0, errors: 0 },
  );
  const today = data.daily.at(-1)?.requests ?? 0;
  const week = data.daily.slice(-7).reduce((a, d) => a + d.requests, 0);
  const busiest = data.daily.reduce<{ day: string; requests: number } | null>((b, d) => (d.requests > (b?.requests ?? 0) ? d : b), null);
  const activeWeek = data.keys.filter((k) => k.usage.week > 0).length;
  const neverUsed = data.keys.filter((k) => k.last_used == null).length;
  const stranded = data.keys.filter((k) => k.owner?.disabled);
  const away = data.turned_away;

  const count = (n: number) => (
    <Text color={n ? undefined : "secondary"} hasTabularNumbers>
      {fmtNum(n)}
    </Text>
  );
  const columns: TableColumn<KeyRow>[] = [
    {
      key: "name",
      header: "Key",
      sortable: true,
      width: proportional(1.5, { minWidth: 170 }),
      renderCell: (k) => (
        <VStack gap={0}>
          <Text weight="medium" maxLines={1}>
            {k.name}{" "}
            <Text type="supporting" color="secondary">
              …{k.hint}
            </Text>
          </Text>
          <Tooltip content={`Reads ${k.projects || "no project"}`}>
            <Text type="supporting" color="secondary" maxLines={1}>
              {props.people || k.ownerDisabled ? `${k.owner}${k.ownerDisabled ? " (disabled)" : ""} · ` : ""}
              {k.projects || "no project"}
            </Text>
          </Tooltip>
        </VStack>
      ),
    },
    ...(narrow
      ? []
      : [
          { key: "today", header: "Today", sortable: true, width: pixel(72), align: "end" as const, renderCell: (k: KeyRow) => count(k.today) },
          { key: "week", header: "7 days", sortable: true, width: pixel(80), align: "end" as const, renderCell: (k: KeyRow) => count(k.week) },
        ]),
    {
      key: "month",
      header: `${DAYS} days`,
      sortable: true,
      width: pixel(156),
      align: "end",
      renderCell: (k) => (
        <Tooltip content={usageLine(k.usage)}>
          <VStack gap={0} hAlign="end">
            {count(k.month)}
            {(k.usage.queries > 0 || k.usage.errors > 0) && (
              <Text type="supporting" color="secondary" hasTabularNumbers maxLines={1}>
                {[k.usage.queries ? `${fmtNum(k.usage.queries)} quer${k.usage.queries === 1 ? "y" : "ies"}` : "", failedShare(k.usage) ? `${failedShare(k.usage)} failed` : ""]
                  .filter(Boolean)
                  .join(" · ")}
              </Text>
            )}
          </VStack>
        </Tooltip>
      ),
    },
    {
      key: "last_used",
      header: "Last used",
      sortable: true,
      width: pixel(104),
      align: "end",
      renderCell: (k) => (
        <Tooltip content={`Made ${new Date(k.created_at * 1000).toLocaleDateString()}`}>
          <Text type="supporting" color="secondary" hasTabularNumbers>
            {k.last_used ? ago(k.last_used) : "never"}
          </Text>
        </Tooltip>
      ),
    },
    {
      key: "id",
      header: "",
      width: pixel(48),
      align: "end",
      renderCell: (k) => (
        <IconButton size="sm" variant="ghost" label={`Revoke ${k.name}`} tooltip="Revoke" icon={<IconTrash />} onClick={() => setRevoking(k)} />
      ),
    },
  ];

  return (
    <VStack gap={8}>
      <HStack gap={2} vAlign="center">
        <Text type="supporting" color="secondary" xstyle={styles.grow}>
          Data API keys read chosen projects' outputs from apps elsewhere (people make them in the account menu). Requests are counted by day,
          in UTC; another server sharing this hub's database adds its count within a minute.
        </Text>
        <Button size="sm" variant="secondary" xstyle={styles.keep} label="Refresh" tooltip="Count again" isLoading={busy} onClick={load} />
      </HStack>
      {err && <Banner role="alert" status="error" title={`Couldn't count again: ${err}`} />}
      {stranded.length > 0 && (
        <Banner
          status="warning"
          title={`${stranded.length} key${stranded.length === 1 ? "" : "s"} of disabled people`}
          description={`${stranded.map((k) => k.name).join(", ")}: a key never reads more than its maker, so these read nothing. Revoke them unless the account comes back.`}
        />
      )}
      {away.week > 0 && (
        <Banner
          status="warning"
          title={`${fmtNum(away.week)} request${away.week === 1 ? "" : "s"} this week with an unknown or revoked key`}
          description="An app still using a revoked key, or someone trying keys. Each was turned away."
        />
      )}

      <MetadataList columns="multi">
        <MetadataListItem label="Keys">
          <Text hasTabularNumbers>
            {fmtNum(data.keys.length)}
            {data.keys.length ? `, ${fmtNum(activeWeek)} used this week` : ""}
          </Text>
        </MetadataListItem>
        <MetadataListItem label="Requests today">
          <Text hasTabularNumbers>{fmtNum(today)}</Text>
        </MetadataListItem>
        <MetadataListItem label="Last 7 days">
          <Text hasTabularNumbers>{fmtNum(week)}</Text>
        </MetadataListItem>
        <MetadataListItem label={`Last ${DAYS} days`}>
          <Text hasTabularNumbers>{usageLine(total)}</Text>
        </MetadataListItem>
        <MetadataListItem label="Failed">
          <Text hasTabularNumbers>{failedShare(total) ? `${failedShare(total)} of requests` : "none"}</Text>
        </MetadataListItem>
        <MetadataListItem label="Busiest day">
          <Text hasTabularNumbers>{busiest ? `${fmtNum(busiest.requests)} on ${dayLabel(busiest.day)}` : "—"}</Text>
        </MetadataListItem>
        <MetadataListItem label="Turned away">
          <Text hasTabularNumbers>{away.requests ? `${fmtNum(away.requests)} in ${DAYS} days` : "none"}</Text>
        </MetadataListItem>
        {neverUsed > 0 && (
          <MetadataListItem label="Never used">
            <Text hasTabularNumbers>
              {fmtNum(neverUsed)} key{neverUsed === 1 ? "" : "s"}
            </Text>
          </MetadataListItem>
        )}
      </MetadataList>

      <VStack gap={3}>
        <VStack gap={1}>
          <Heading level={2}>Keys</Heading>
          <Text type="supporting" color="secondary">
            Most used first. A failed request is one the key made that was answered with an error: a bad query, a missing node, a project its
            maker can no longer open.
          </Text>
        </VStack>
        {rows.length === 0 ? (
          <Text color="secondary">No data API keys yet. Anyone signed in makes one in the account menu, under Data API keys.</Text>
        ) : (
          <Table<KeyRow>
            data={sortedData}
            columns={columns}
            idKey="id"
            density="compact"
            textOverflow="truncate"
            plugins={{ sort: sortPlugin }}
            xstyle={styles.table}
          />
        )}
      </VStack>

      <AlertDialog
        isOpen={!!revoking}
        onOpenChange={(open) => {
          if (!open) setRevoking(null);
        }}
        title={revoking ? `Revoke ${revoking.name}?` : "Revoke this key?"}
        description="Any app using it stops getting data at once. This can't be undone: its maker would make a new key and give it to the app."
        actionLabel="Revoke"
        actionVariant="destructive"
        onAction={() => {
          const k = revoking;
          setRevoking(null);
          if (k) void revoke(k);
        }}
      />
    </VStack>
  );
}

const styles = stylex.create({
  grow: { flexGrow: 1, minWidth: 0 },
  // The button keeps its label beside a long line.
  keep: { flexShrink: 0 },
  table: { minWidth: 0 },
});
