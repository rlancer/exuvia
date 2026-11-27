import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { Link } from "@astryxdesign/core/Link";
import { MetadataList, MetadataListItem } from "@astryxdesign/core/MetadataList";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Spinner } from "@astryxdesign/core/Spinner";
import { pixel, proportional, Table, useTableSortable, useTableSortableState, type TableColumn } from "@astryxdesign/core/Table";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { VStack } from "@astryxdesign/core/VStack";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import { adminApi, type HubListing, type HubStorage, type HubStorageKind } from "./api";
import { files } from "./Storage";
import { ago, fmtBytes, fmtNum } from "./util";

/** Below this share of the disk free, the page warns. */
export const LOW_DISK = 0.1;

const KIND_LABEL: Record<HubStorageKind, { label: string; hint: string }> = {
  outputs: { label: "Node outputs", hint: "Every output a run stored, current and earlier: exuvia never deletes one." },
  code: { label: "Project files", hint: "Nodes, views, helpers and each project's settings." },
  database: { label: "Records databases", hint: "Each project's runs, schedules, conversations and code versions." },
  caches: { label: "Project caches", hint: "Unpacked parts, runs being committed, and any install of a project's own." },
  other: { label: "Other in projects", hint: "Everything else in the projects' .exuvia/: their secrets keys and the like." },
};

const SHARED_HINT: Record<string, string> = {
  packages: "The Python packages the projects import, installed once for every project that asks for the same ones.",
  npm: "Modules the projects' views import, fetched once and kept.",
  "hub.db": "Users, sharing, groups, the hub's variables and MCP servers.",
};

/** A row of "Where the space goes": a kind of space in the projects, or a folder the hub shares between them. */
export type SpacePlace = { key: string; label: string; hint: string; bytes: number; files: number; shared: boolean };

/** Every kind of space on the hub, the projects' and the shared folders', largest first; empty ones are left out. */
export function hubPlaces(s: Pick<HubStorage, "kinds" | "shared">): SpacePlace[] {
  const out: SpacePlace[] = (Object.keys(KIND_LABEL) as HubStorageKind[]).map((k) => ({
    key: k,
    ...KIND_LABEL[k],
    ...s.kinds[k],
    shared: false,
  }));
  for (const p of s.shared)
    out.push({
      key: `shared:${p.key}`,
      label: p.key === "other" ? "Other in the hub's folder" : p.label,
      hint: SHARED_HINT[p.key] ?? "The hub's own secrets key and the like.",
      bytes: p.bytes,
      files: p.files,
      shared: true,
    });
  return out.filter((p) => p.files > 0).sort((a, b) => b.bytes - a.bytes || a.key.localeCompare(b.key));
}

/** Whether the disk is low: less than LOW_DISK of it free. */
export const diskLow = (d: HubStorage["disk"]) => !!d && d.total > 0 && d.free / d.total < LOW_DISK;

/** A project's row in "By project": flat, so the table sorts on any column. */
type ProjectRow = {
  id: string;
  name: string;
  nodes: number;
  owner: string;
  group: string;
  bytes: number;
  outputs: number;
  output_files: number;
  outputs_nodes: number;
  database: number;
  code: number;
  rest: number;
  written_at: number;
};

type LargestRow = { key: string; project: string; name: string; node: string; bytes: number; files: number };

const storageUrl = (id: string) => `/p/${encodeURIComponent(id)}/#storage`;

/**
 * The admin page's Storage section: what the whole hub keeps on disk. The disk itself, where the space
 * goes (the projects' outputs, files, databases and caches, and the folders the hub shares between them),
 * every project's size, and the largest outputs on the hub. Measured from the disk when the section opens
 * (exuvia/storage.py hub_report), and again on Refresh.
 */
export function HubStoragePanel(props: { listing: HubListing; people: boolean; self: string | null }) {
  const [data, setData] = useState<HubStorage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
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
    adminApi.storage().then(
      (s) => live.current && (setData(s), setErr(null), setBusy(false)),
      (e: Error) => live.current && (setErr(e.message), setBusy(false)),
    );
  }, []);
  useEffect(() => load(), [load]);

  const known = useMemo(() => new Map(props.listing.projects.map((p) => [p.id, p])), [props.listing]);
  const rows: ProjectRow[] = useMemo(
    () =>
      (data?.projects ?? []).map((p) => {
        const listed = known.get(p.id);
        const k = p.kinds;
        return {
          id: p.id,
          name: listed?.name ?? p.name,
          nodes: p.nodes,
          owner: listed?.owner ? (listed.owner.id === props.self ? `${listed.owner.name} (you)` : listed.owner.name) : "",
          group: listed?.group?.name ?? "",
          bytes: p.bytes,
          outputs: k.outputs.bytes,
          output_files: k.outputs.files,
          outputs_nodes: p.outputs_nodes,
          database: k.database.bytes,
          code: k.code.bytes,
          rest: k.caches.bytes + k.other.bytes,
          written_at: p.written_at ?? 0,
        };
      }),
    [data, known, props.self],
  );
  const { sortedData, sortConfig } = useTableSortableState<ProjectRow>({
    data: rows,
    defaultSort: [{ sortKey: "bytes", direction: "descending" }],
  });
  const sortPlugin = useTableSortable<ProjectRow>(sortConfig);

  if (err && !data) return <Banner role="alert" status="error" title="Couldn't measure the hub" description={err} endContent={<Button size="sm" label="Try again" onClick={load} />} />;
  if (!data)
    return (
      <Center padding={6}>
        <Spinner label="Measuring every project…" />
      </Center>
    );

  const total = Math.max(data.total_bytes, 1);
  const disk = data.disk;
  const share = (bytes: number, label: string) => (
    <ProgressBar label={`${label}: share of the hub`} isLabelHidden value={bytes} max={total} variant="neutral" />
  );
  const outputs = data.kinds.outputs;

  const placeColumns: TableColumn<SpacePlace>[] = [
    {
      key: "label",
      header: "What",
      width: proportional(2, { minWidth: 180 }),
      renderCell: (p) => (
        <VStack gap={0.5}>
          <Text>{p.label}</Text>
          <Text type="supporting" color="secondary" maxLines={narrow ? 2 : 1}>
            {p.hint}
          </Text>
        </VStack>
      ),
    },
    {
      key: "bytes",
      header: "Size",
      width: pixel(96),
      align: "end",
      renderCell: (p) => <Text hasTabularNumbers>{fmtBytes(p.bytes)}</Text>,
    },
    ...(narrow
      ? []
      : [
          {
            key: "files",
            header: "Files",
            width: pixel(96),
            align: "end" as const,
            renderCell: (p: SpacePlace) => (
              <Text type="supporting" color="secondary" hasTabularNumbers>
                {fmtNum(p.files)}
              </Text>
            ),
          },
          { key: "share", header: "Share", width: proportional(1, { minWidth: 120 }), renderCell: (p: SpacePlace) => share(p.bytes, p.label) },
        ]),
  ];

  const projectColumns: TableColumn<ProjectRow>[] = [
    {
      key: "name",
      header: "Project",
      sortable: true,
      width: proportional(1.4, { minWidth: 150 }),
      renderCell: (p) => (
        <VStack gap={0}>
          <Link href={storageUrl(p.id)} tooltip="Open its storage page">
            {p.name}
          </Link>
          <Text type="supporting" color="secondary" maxLines={1}>
            {[p.id, `${p.nodes} node${p.nodes === 1 ? "" : "s"}`, props.people && p.owner ? p.owner : "", p.group ? `in ${p.group}` : ""]
              .filter(Boolean)
              .join(" · ")}
          </Text>
        </VStack>
      ),
    },
    {
      key: "bytes",
      header: "On disk",
      sortable: true,
      width: proportional(1, { minWidth: 120 }),
      renderCell: (p) => (
        <Tooltip
          content={`Outputs ${fmtBytes(p.outputs)}, records ${fmtBytes(p.database)}, project files ${fmtBytes(p.code)}, caches and other ${fmtBytes(p.rest)}`}
        >
          <VStack gap={1}>
            <Text hasTabularNumbers>{fmtBytes(p.bytes)}</Text>
            {share(p.bytes, p.name)}
          </VStack>
        </Tooltip>
      ),
    },
    {
      key: "outputs",
      header: "Outputs",
      sortable: true,
      width: pixel(120),
      align: "end",
      renderCell: (p) =>
        p.output_files ? (
          <VStack gap={0} hAlign="end">
            <Text hasTabularNumbers>{fmtBytes(p.outputs)}</Text>
            <Text type="supporting" color="secondary" hasTabularNumbers>
              {files(p.output_files)}
            </Text>
          </VStack>
        ) : (
          <Text type="supporting" color="secondary">
            none yet
          </Text>
        ),
    },
    ...(narrow
      ? []
      : [
          {
            key: "database",
            header: "Records",
            sortable: true,
            width: pixel(96),
            align: "end" as const,
            renderCell: (p: ProjectRow) => (
              <Text color={p.database ? undefined : "secondary"} hasTabularNumbers>
                {p.database ? fmtBytes(p.database) : "—"}
              </Text>
            ),
          },
          {
            key: "written_at",
            header: "Last output",
            sortable: true,
            width: pixel(110),
            align: "end" as const,
            renderCell: (p: ProjectRow) => (
              <Text type="supporting" color="secondary" hasTabularNumbers>
                {ago(p.written_at) || "—"}
              </Text>
            ),
          },
        ]),
  ];

  const largest: LargestRow[] = data.largest.map((o) => ({
    ...o,
    key: `${o.project}\u0000${o.node}`,
    name: known.get(o.project)?.name ?? o.project,
  }));
  const largestColumns: TableColumn<LargestRow>[] = [
    {
      key: "node",
      header: "Node",
      width: proportional(1.4, { minWidth: 150 }),
      renderCell: (o) => (
        <VStack gap={0}>
          <Text weight="medium" maxLines={1}>
            {o.node}
          </Text>
          <Link href={storageUrl(o.project)} tooltip="Open the project's storage page">
            {o.name}
          </Link>
        </VStack>
      ),
    },
    {
      key: "bytes",
      header: "Size",
      width: pixel(120),
      align: "end",
      renderCell: (o) => (
        <VStack gap={0} hAlign="end">
          <Text hasTabularNumbers>{fmtBytes(o.bytes)}</Text>
          <Tooltip content="Each run's output is kept, so this is one file per run (or per partition chunk)">
            <Text type="supporting" color="secondary" hasTabularNumbers>
              {files(o.files)}
            </Text>
          </Tooltip>
        </VStack>
      ),
    },
    ...(narrow
      ? []
      : [{ key: "share", header: "Share", width: proportional(1, { minWidth: 120 }), renderCell: (o: LargestRow) => share(o.bytes, o.node) }]),
  ];

  const noProjects = data.projects.length === 0;
  return (
    <VStack gap={8}>
      <HStack gap={2} vAlign="center">
        <Text type="supporting" color="secondary" xstyle={styles.grow}>
          Measured from the disk {ago(data.generated_at)}. Sizes are what is on this server; it never reads a row of an output to count them.
        </Text>
        <Button size="sm" variant="secondary" xstyle={styles.keep} label="Refresh" tooltip="Measure again" isLoading={busy} onClick={load} />
      </HStack>
      {err && <Banner role="alert" status="error" title={`Couldn't measure again: ${err}`} />}
      {diskLow(disk) && disk && (
        <Banner
          role="alert"
          status="warning"
          title={`The disk is nearly full: ${fmtBytes(disk.free)} free of ${fmtBytes(disk.total)}`}
          description="Runs fail once it fills. Node outputs are usually most of it: exuvia keeps every run's, so delete projects nobody needs, or move outputs to an artifact store (EXUVIA_ARTIFACT_STORE)."
        />
      )}

      <MetadataList columns="multi">
        <MetadataListItem label="The hub">
          <Text hasTabularNumbers>{fmtBytes(data.total_bytes)}</Text>
        </MetadataListItem>
        {disk && (
          <MetadataListItem label="Disk">
            <VStack gap={1}>
              <Text hasTabularNumbers>
                {fmtBytes(disk.free)} free of {fmtBytes(disk.total)}
              </Text>
              <ProgressBar
                label="Disk used"
                isLabelHidden
                value={disk.total - disk.free}
                max={Math.max(disk.total, 1)}
                variant={diskLow(disk) ? "warning" : "accent"}
              />
            </VStack>
          </MetadataListItem>
        )}
        <MetadataListItem label="Projects">
          <Text hasTabularNumbers>
            {fmtBytes(data.projects_bytes)} in {fmtNum(data.projects.length)} project{data.projects.length === 1 ? "" : "s"}
          </Text>
        </MetadataListItem>
        <MetadataListItem label="Node outputs">
          <Text hasTabularNumbers>
            {fmtBytes(outputs.bytes)} in {files(outputs.files)}
          </Text>
        </MetadataListItem>
        <MetadataListItem label="Shared between projects">
          <Text hasTabularNumbers>{fmtBytes(data.shared_bytes)}</Text>
        </MetadataListItem>
        <MetadataListItem label="Records">
          <Text>{data.database.where}</Text>
        </MetadataListItem>
        <MetadataListItem label="Outputs kept in">
          <Text>{data.artifact_store.configured ? (data.artifact_store.where ?? "an artifact store") : "this server's disk"}</Text>
        </MetadataListItem>
      </MetadataList>

      {data.artifact_store.configured && (
        <Banner
          status="info"
          title="Outputs live in the artifact store"
          description="What is counted here is this server's copy of them, fetched as they are read. Their full size is the bucket's."
        />
      )}
      {data.database.shared && (
        <Text type="supporting" color="secondary">
          Run history, schedules and chats are in a shared database ({data.database.where}), so the projects' own records count nothing here.
        </Text>
      )}

      <VStack gap={3}>
        <Heading level={2}>Where the space goes</Heading>
        <Table<SpacePlace> data={hubPlaces(data)} columns={placeColumns} idKey="key" density="compact" xstyle={styles.table} />
      </VStack>

      <VStack gap={3}>
        <VStack gap={1}>
          <Heading level={2}>By project</Heading>
          <Text type="supporting" color="secondary">
            Largest first. Open one for its outputs node by node, their compression and its records.
          </Text>
        </VStack>
        {noProjects ? (
          <Text color="secondary">No projects yet.</Text>
        ) : (
          <Table<ProjectRow>
            data={sortedData}
            columns={projectColumns}
            idKey="id"
            density="compact"
            textOverflow="truncate"
            plugins={{ sort: sortPlugin }}
            xstyle={styles.table}
          />
        )}
      </VStack>

      {largest.length > 0 && (
        <VStack gap={3}>
          <VStack gap={1}>
            <Heading level={2}>Largest outputs</Heading>
            <Text type="supporting" color="secondary">
              The nodes whose stored outputs take the most space, every run of each kept, across the hub.
            </Text>
          </VStack>
          <Table<LargestRow> data={largest} columns={largestColumns} idKey="key" density="compact" textOverflow="truncate" xstyle={styles.table} />
        </VStack>
      )}
    </VStack>
  );
}

const styles = stylex.create({
  grow: { flexGrow: 1, minWidth: 0 },
  // The button keeps its label beside a long line.
  keep: { flexShrink: 0 },
  table: { minWidth: 0 },
});
