import { useCallback, useEffect, useRef, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Link } from "@astryxdesign/core/Link";
import { MetadataList, MetadataListItem } from "@astryxdesign/core/MetadataList";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { pixel, proportional, Table, type TableColumn } from "@astryxdesign/core/Table";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { VStack } from "@astryxdesign/core/VStack";
import { api, type StorageNode, type StoragePart, type StoragePartitioned, type StorageState } from "./api";
import { IconClose, IconDisk } from "./icons";
import { ago, fmtBytes, fmtNum } from "./util";

/** How long the page waits after a graph or run event before reading again. */
const SETTLE_MS = 2000;

/** "3.4×": how many times smaller compression made the data; "" when it can't be told. */
export function ratio(uncompressed: number, stored: number): string {
  if (!uncompressed || !stored) return "";
  const r = uncompressed / stored;
  return `${r.toLocaleString(undefined, { maximumFractionDigits: r < 10 ? 1 : 0 })}×`;
}

/** "12 files", "1 file". */
export function files(n: number): string {
  return `${fmtNum(n)} ${n === 1 ? "file" : "files"}`;
}

/** A row of "Where the space goes": a part of `.exuvia/`, or the project's own files. */
type Place = { key: string; label: string; hint: string; bytes: number; files: number };

const HINTS: Record<string, string> = {
  artifacts: "Every output a run stored, current and earlier: exuvia never deletes one.",
  parts: "Outputs with several parts, unpacked to be read. Rebuilt when missing.",
  packages: "The Python packages your nodes import, installed for this project.",
  npm: "Modules views import, fetched once and kept.",
  staging: "Rows a partitioned run returned, while they are committed.",
  "exuvia.db": "Runs, schedules, conversations and every version of the code.",
  other: "Everything else in .exuvia/: the secrets key and the like.",
  code: "Nodes, views, helpers and the project's settings.",
};

export function places(s: StorageState): Place[] {
  const out: Place[] = s.state.map((p: StoragePart) => ({ ...p, hint: HINTS[p.key] ?? HINTS.other }));
  out.push({ key: "code", label: "Project files", hint: HINTS.code, bytes: s.code.bytes, files: s.code.files });
  return out.sort((a, b) => b.bytes - a.bytes || a.key.localeCompare(b.key));
}

/**
 * Storage mode: what the project keeps on disk. Where the space goes, each
 * node's outputs (the current one and those kept from earlier runs), the
 * Parquet files' compression, each partitioned output against its
 * compaction limit, and the records database.
 */
export function Storage(props: {
  /** Changes on every graph or run event: read the figures again, once they settle. */
  tick: unknown;
  onOpenNode: (name: string) => void;
  /** Back to the graph: the page is opened from the side panel's Settings tab. */
  onClose: () => void;
}) {
  const [data, setData] = useState<StorageState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const load = useCallback(() => {
    setBusy(true);
    return api.storage().then(
      (s) => live.current && (setData(s), setErr(null), setBusy(false)),
      (e: Error) => live.current && (setErr(e.message), setBusy(false)),
    );
  }, []);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      void load();
      return;
    }
    const t = window.setTimeout(() => void load(), SETTLE_MS);
    return () => window.clearTimeout(t);
  }, [props.tick, load]);

  const open = (n: StorageNode) =>
    n.exists ? (
      <Link onClick={() => props.onOpenNode(n.name)} tooltip="Open it on the graph">
        {n.name}
      </Link>
    ) : (
      <Tooltip content="This node was deleted; its outputs are still kept">
        <Text color="secondary">{n.name}</Text>
      </Tooltip>
    );

  const total = data ? Math.max(data.total_bytes, 1) : 1;
  const placeColumns: TableColumn<Place>[] = [
    {
      key: "label",
      header: "What",
      width: proportional(2, { minWidth: 180 }),
      renderCell: (p) => (
        <VStack gap={0.5}>
          <Text>{p.label}</Text>
          <Text type="supporting" color="secondary" maxLines={1}>
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
    {
      key: "files",
      header: "Files",
      width: pixel(96),
      align: "end",
      renderCell: (p) => (
        <Text type="supporting" color="secondary" hasTabularNumbers>
          {fmtNum(p.files)}
        </Text>
      ),
    },
    {
      key: "share",
      header: "Share",
      width: proportional(1, { minWidth: 120 }),
      renderCell: (p) => (
        <ProgressBar label={`${p.label}: share of the project`} isLabelHidden value={p.bytes} max={total} variant="neutral" />
      ),
    },
  ];

  const nodeColumns: TableColumn<StorageNode>[] = [
    {
      key: "name",
      header: "Node",
      width: proportional(1.4, { minWidth: 140 }),
      renderCell: (n) => (
        <VStack gap={0.5}>
          {open(n)}
          {n.partitioned && (
            <Text type="supporting" color="secondary">
              partitioned
            </Text>
          )}
        </VStack>
      ),
    },
    {
      key: "bytes",
      header: "On disk",
      width: proportional(1, { minWidth: 110 }),
      align: "end",
      renderCell: (n) =>
        n.files ? (
          <VStack gap={0.5} hAlign="end">
            <Text hasTabularNumbers>{fmtBytes(n.bytes)}</Text>
            <Text type="supporting" color="secondary" hasTabularNumbers>
              {files(n.files)}
            </Text>
          </VStack>
        ) : (
          <Text type="supporting" color="secondary">
            no output yet
          </Text>
        ),
    },
    {
      key: "current",
      header: "Current output",
      width: proportional(1, { minWidth: 120 }),
      align: "end",
      renderCell: (n) =>
        n.current_files ? (
          <VStack gap={0.5} hAlign="end">
            <Text hasTabularNumbers>{fmtBytes(n.current_bytes)}</Text>
            {n.rows > 0 && (
              <Text type="supporting" color="secondary" hasTabularNumbers>
                {fmtNum(n.rows)} rows
              </Text>
            )}
          </VStack>
        ) : (
          <Text type="supporting" color="secondary">
            —
          </Text>
        ),
    },
    {
      key: "earlier",
      header: "Earlier runs",
      width: proportional(1, { minWidth: 110 }),
      align: "end",
      renderCell: (n) =>
        n.files > n.current_files ? (
          <Tooltip content="Outputs of earlier runs (and older parameter sets' outputs), kept so each run can be read again">
            <VStack gap={0.5} hAlign="end">
              <Text hasTabularNumbers>{fmtBytes(n.bytes - n.current_bytes)}</Text>
              <Text type="supporting" color="secondary" hasTabularNumbers>
                {files(n.files - n.current_files)}
              </Text>
            </VStack>
          </Tooltip>
        ) : (
          <Text type="supporting" color="secondary">
            —
          </Text>
        ),
    },
    {
      key: "compression",
      header: "Compression",
      width: proportional(0.9, { minWidth: 100 }),
      align: "end",
      renderCell: (n) =>
        n.parquet_bytes ? (
          <Tooltip content={`${fmtBytes(n.uncompressed_bytes)} of column data, compressed to ${fmtBytes(n.compressed_bytes)}`}>
            <Text hasTabularNumbers>{ratio(n.uncompressed_bytes, n.compressed_bytes) || "—"}</Text>
          </Tooltip>
        ) : (
          <Text type="supporting" color="secondary">
            —
          </Text>
        ),
    },
    {
      key: "runs",
      header: "Runs",
      width: pixel(72),
      align: "end",
      renderCell: (n) => (
        <Text type="supporting" color="secondary" hasTabularNumbers>
          {fmtNum(n.runs)}
        </Text>
      ),
    },
  ];

  const compactColumns: TableColumn<StoragePartitioned>[] = [
    {
      key: "node",
      header: "Output",
      width: proportional(1.4, { minWidth: 140 }),
      renderCell: (p) => (
        <VStack gap={0.5}>
          <Link onClick={() => props.onOpenNode(p.node)} tooltip="Open it on the graph">
            {p.node}
          </Link>
          {p.params_key && (
            <Text type="supporting" color="secondary" maxLines={1}>
              {p.params_key}
            </Text>
          )}
        </VStack>
      ),
    },
    {
      key: "partitions",
      header: "Partitions",
      width: pixel(96),
      align: "end",
      renderCell: (p) => <Text hasTabularNumbers>{fmtNum(p.partitions)}</Text>,
    },
    {
      key: "files",
      header: "Files / compaction limit",
      width: proportional(1.6, { minWidth: 180 }),
      renderCell: (p) => (
        <Tooltip
          content={`${files(p.files)} now; it merges each ${p.periods > 1 ? "year's" : "group's"} files into one once it has more than ${fmtNum(p.limit)} (one per ${p.periods > 1 ? "year" : "group"}, ${p.periods > 1 ? `${p.periods} years, ` : ""}plus ${fmtNum(p.limit - p.periods)})`}
        >
          <ProgressBar
            label={`${p.node}: files against its compaction limit`}
            isLabelHidden
            hasValueLabel
            value={Math.min(p.files, p.limit)}
            max={p.limit}
            formatValueLabel={() => `${fmtNum(p.files)} / ${fmtNum(p.limit)}`}
            variant={p.files >= p.limit ? "warning" : "accent"}
          />
        </Tooltip>
      ),
    },
    {
      key: "merged",
      header: "Merged files",
      width: pixel(110),
      align: "end",
      renderCell: (p) => (
        <Tooltip content="Files holding more than one partition: what a backfill or compaction wrote">
          <Text hasTabularNumbers>{fmtNum(p.merged_files)}</Text>
        </Tooltip>
      ),
    },
    {
      key: "superseded",
      header: "Replaced chunks",
      width: pixel(130),
      align: "end",
      renderCell: (p) => (
        <Tooltip content="Chunks earlier versions read and this one doesn't (merged or corrected since). Kept, so every earlier run can be read again.">
          <Text type="supporting" color="secondary" hasTabularNumbers>
            {fmtNum(p.superseded_files)}
          </Text>
        </Tooltip>
      ),
    },
    {
      key: "version",
      header: "Version",
      width: pixel(80),
      align: "end",
      renderCell: (p) => (
        <Text type="supporting" color="secondary" hasTabularNumbers>
          {fmtNum(p.version)}
        </Text>
      ),
    },
  ];

  const pq = data?.parquet;
  const out = data?.outputs;
  const rows = data?.database.rows;

  return (
    <section {...stylex.props(styles.page)}>
      <div {...stylex.props(styles.head)}>
        <IconDisk />
        <Heading level={4} accessibilityLevel={2}>
          Storage
        </Heading>
        <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.hint}>
          What this project keeps on disk{data ? `, as of ${ago(data.generated_at)}` : ""}.
        </Text>
        <span {...stylex.props(styles.grow)} />
        <Button size="sm" variant="ghost" label="Refresh" tooltip="Measure again" isLoading={busy} onClick={() => void load()} />
        <IconButton variant="ghost" size="sm" label="Close storage" tooltip="Close and go back to the graph" icon={<IconClose />} onClick={props.onClose} />
      </div>
      <div {...stylex.props(styles.body)}>
        {err && <Banner status="error" title={`Couldn't measure the project: ${err}`} />}
        {data && out && pq && rows && (
          <>
            <MetadataList columns="multi">
              <MetadataListItem label="On disk">
                <Text hasTabularNumbers>{fmtBytes(data.total_bytes)}</Text>
              </MetadataListItem>
              <MetadataListItem label="Node outputs">
                <Text hasTabularNumbers>
                  {fmtBytes(out.bytes)} in {files(out.files)}
                </Text>
              </MetadataListItem>
              <MetadataListItem label="Current outputs">
                <Text hasTabularNumbers>
                  {fmtBytes(out.current_bytes)} in {files(out.current_files)}
                </Text>
              </MetadataListItem>
              <MetadataListItem label="Parquet compression">
                <Text hasTabularNumbers>
                  {pq.files ? `${ratio(pq.uncompressed_bytes, pq.compressed_bytes) || "—"} ${pq.codecs.map((c) => c.codec.toLowerCase()).join(", ")}` : "—"}
                </Text>
              </MetadataListItem>
              <MetadataListItem label="Records database">
                <Text hasTabularNumbers>
                  {data.database.bytes != null ? fmtBytes(data.database.bytes) : data.database.kind === "postgres" ? "Postgres (shared)" : "shared"}
                </Text>
              </MetadataListItem>
              {data.remote.configured && (
                <MetadataListItem label="Artifact store">
                  <Text hasTabularNumbers>{files(data.remote.uploaded)} uploaded</Text>
                </MetadataListItem>
              )}
              {data.remote.cache_limit != null && (
                <MetadataListItem label="Local copy">
                  <Text hasTabularNumbers>up to {fmtBytes(data.remote.cache_limit)}, least recently read first to go</Text>
                </MetadataListItem>
              )}
            </MetadataList>

            {data.remote.missing > 0 && (
              <Banner
                status={data.remote.configured ? "info" : "warning"}
                title={
                  data.remote.configured
                    ? `${files(data.remote.missing)} only in the artifact store`
                    : `${files(data.remote.missing)} missing from disk`
                }
                description={
                  data.remote.configured
                    ? "They are fetched the first time something reads them; their sizes aren't counted here."
                    : "Runs recorded these outputs, but they aren't in .exuvia/artifacts any more. Run their nodes again to rebuild them."
                }
              />
            )}

            <VStack gap={2}>
              <Heading level={5} accessibilityLevel={3}>
                Where the space goes
              </Heading>
              <Table<Place> data={places(data)} columns={placeColumns} idKey="key" density="compact" />
            </VStack>

            <VStack gap={2}>
              <HStack gap={2} vAlign="center">
                <Heading level={5} accessibilityLevel={3}>
                  Outputs by node
                </Heading>
                <Text type="supporting" color="secondary">
                  Largest first. Every run's output is kept, so a node that runs often grows.
                </Text>
              </HStack>
              <Table<StorageNode> data={data.nodes} columns={nodeColumns} idKey="name" density="compact" />
            </VStack>

            {pq.files > 0 && (
              <VStack gap={2}>
                <Heading level={5} accessibilityLevel={3}>
                  Parquet
                </Heading>
                <MetadataList columns="multi">
                  <MetadataListItem label="Files">
                    <Text hasTabularNumbers>{fmtNum(pq.files)}</Text>
                  </MetadataListItem>
                  <MetadataListItem label="Rows">
                    <Text hasTabularNumbers>{fmtNum(pq.rows)}</Text>
                  </MetadataListItem>
                  <MetadataListItem label="Row groups">
                    <Text hasTabularNumbers>{fmtNum(pq.row_groups)}</Text>
                  </MetadataListItem>
                  <MetadataListItem label="On disk">
                    <Text hasTabularNumbers>{fmtBytes(pq.bytes)}</Text>
                  </MetadataListItem>
                  <MetadataListItem label="Column data">
                    <Text hasTabularNumbers>
                      {fmtBytes(pq.uncompressed_bytes)} compressed to {fmtBytes(pq.compressed_bytes)}
                    </Text>
                  </MetadataListItem>
                  <MetadataListItem label="Codecs">
                    <Text hasTabularNumbers>
                      {pq.codecs.map((c) => `${c.codec.toLowerCase()} ${fmtBytes(c.bytes)}`).join(", ") || "—"}
                    </Text>
                  </MetadataListItem>
                </MetadataList>
                {(out.by_kind.json.files > 0 || out.by_kind.multi.files > 0) && (
                  <Text type="supporting" color="secondary">
                    Beside {files(pq.files)} of Parquet: {out.by_kind.json.files ? `${files(out.by_kind.json.files)} of JSON (${fmtBytes(out.by_kind.json.bytes)})` : ""}
                    {out.by_kind.json.files && out.by_kind.multi.files ? " and " : ""}
                    {out.by_kind.multi.files ? `${files(out.by_kind.multi.files)} with several parts (${fmtBytes(out.by_kind.multi.bytes)})` : ""}.
                  </Text>
                )}
              </VStack>
            )}

            {data.partitioned.length > 0 && (
              <VStack gap={2}>
                <HStack gap={2} vAlign="center">
                  <Heading level={5} accessibilityLevel={3}>
                    Compaction
                  </Heading>
                  <Text type="supporting" color="secondary">
                    A partitioned output merges its files once it has more than {fmtNum(data.compact_after)} beyond one per year (EXUVIA_COMPACT_AFTER).
                  </Text>
                </HStack>
                <Table<StoragePartitioned>
                  data={data.partitioned}
                  columns={compactColumns}
                  idKey={(p: StoragePartitioned) => `${p.node}\u0000${p.params_key}`}
                  density="compact"
                />
              </VStack>
            )}

            <VStack gap={2}>
              <Heading level={5} accessibilityLevel={3}>
                Records
              </Heading>
              <MetadataList columns="multi">
                <MetadataListItem label="Runs">
                  <Text hasTabularNumbers>{fmtNum(rows.runs)}</Text>
                </MetadataListItem>
                <MetadataListItem label="Code versions">
                  <Text hasTabularNumbers>{fmtNum(rows.code_revisions)}</Text>
                </MetadataListItem>
                <MetadataListItem label="Distinct sources">
                  <Text hasTabularNumbers>{fmtNum(rows.code_blobs)}</Text>
                </MetadataListItem>
                <MetadataListItem label="Agent conversations">
                  <Text hasTabularNumbers>{fmtNum(rows.agent_sessions)}</Text>
                </MetadataListItem>
                {rows.output_files > 0 && (
                  <MetadataListItem label="Partition records">
                    <Text hasTabularNumbers>
                      {fmtNum(rows.output_partitions)} partitions, {files(rows.output_files)}
                    </Text>
                  </MetadataListItem>
                )}
              </MetadataList>
            </VStack>
          </>
        )}
      </div>
    </section>
  );
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
});
