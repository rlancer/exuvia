import { useState } from "react";
import { DropdownMenu, type DropdownMenuOption } from "@astryxdesign/core/DropdownMenu";
import { IconButton } from "@astryxdesign/core/IconButton";
import { IconDownload } from "./icons";
import { failed } from "./toast";
import type { GraphNode, ViewOutput } from "./api";
import { codeArchiveUrl, formatRefusal, nodeExportUrl, saveUrl, TABLE_FORMATS, viewExportUrl, type TableFormat } from "./download";

export interface DownloadItem {
  key: string;
  label: string;
  hint?: string;
  /** Why it can't be picked now; null or unset when it can. */
  disabled?: string | null;
  /** Start the download; a rejection shows as a toast. */
  onPick: () => void | Promise<unknown>;
}

export interface DownloadGroup {
  title?: string;
  items: DownloadItem[];
}

/** The four table formats as menu items; Excel is refused for more rows than a sheet holds.
 * `hints` off leaves out each format's line of explanation (a menu of many tables). */
export function tableItems(
  pick: (f: TableFormat) => void | Promise<unknown>,
  rows?: number | null,
  prefix = "",
  hints = true,
): DownloadItem[] {
  return TABLE_FORMATS.map(({ format, label, hint }) => ({
    key: `${prefix}${format}`,
    label,
    hint: hints ? hint : undefined,
    disabled: formatRefusal(format, rows),
    onPick: () => pick(format),
  }));
}

const JSON_HINT = ".json: the value as it was returned";

/**
 * What can be saved of a node's output: a table in any format, a JSON value
 * as JSON; for a node with several outputs, each of them and a zip of all.
 * `runId` picks a past run's output instead of the current one; `rows` is how
 * many rows the table has, when known.
 */
export function outputGroups(node: GraphNode, o: { runId?: string | null; rows?: number | null; title?: string } = {}): DownloadGroup[] {
  const { runId } = o;
  const url = (format: TableFormat | "zip", part?: string) => nodeExportUrl(node.name, format, { part, runId });
  const json = (part?: string): DownloadItem => ({ key: `${part ?? ""}json`, label: "JSON", hint: JSON_HINT, onPick: () => saveUrl(url("json", part)) });
  switch (node.output_kind) {
    case "table":
      return [{ title: o.title, items: tableItems((f) => saveUrl(url(f)), o.rows === undefined ? node.rows : o.rows) }];
    case "json":
      return [{ title: o.title, items: [json()] }];
    case "multi": {
      const parts = node.schema ?? [];
      return [
        ...parts.map((p) => ({
          title: `${node.name}.${p.name}`,
          items: p.type === "table" ? tableItems((f) => saveUrl(url(f, p.name)), undefined, `${p.name}.`) : [json(p.name)],
        })),
        {
          title: parts.length ? undefined : o.title,
          items: [{ key: "zip", label: "All outputs (.zip)", hint: "every output as it is stored: .parquet and .json files", onPick: () => saveUrl(url("zip")) }],
        },
      ];
    }
    default:
      return [];
  }
}

/** What can be saved of one output a view reads, for the parameter set it shows (one group per table or JSON value). */
export function viewOutputGroups(view: string, out: ViewOutput, params: Record<string, unknown>): DownloadGroup[] {
  if (!out.artifact_id) return [];
  const url = (f: TableFormat, part?: string) => viewExportUrl(view, out.node, f, { part, params });
  const json = (part?: string): DownloadItem => ({ key: "json", label: "JSON", hint: JSON_HINT, onPick: () => saveUrl(url("json", part)) });
  if (out.kind === "table") return [{ title: out.node, items: tableItems((f) => saveUrl(url(f)), undefined, "", false) }];
  if (out.kind === "json") return [{ title: out.node, items: [json()] }];
  if (out.kind === "multi")
    return (out.parts ?? []).map((p) => ({
      title: `${out.node}.${p.name}`,
      items: p.kind === "table" ? tableItems((f) => saveUrl(url(f, p.name)), p.rows, "", false) : [json(p.name)],
    }));
  return [];
}

/** The code of the whole project, or of one node (with the helpers it imports) or one view, as a zip. */
export function codeGroup(o: { node?: string; view?: string; title?: string } = {}): DownloadGroup {
  const hint = o.node
    ? "the node's file and the lib/ helpers it imports"
    : o.view
      ? "every file of the view"
      : "nodes, lib/ helpers, views, exuvia.json and pyproject.toml";
  return { title: o.title ?? "Code", items: [{ key: "code", label: "Code (.zip)", hint, onPick: () => saveUrl(codeArchiveUrl(o)) }] };
}

/**
 * A "Download" button and its menu of what can be saved from here: each group
 * under its title, each item with a line that says what it is, or why it
 * can't be picked now. The menu stays open while a download is prepared.
 */
export function DownloadMenu(props: {
  groups: DownloadGroup[];
  /** Button text; unset shows the icon alone. */
  label?: string;
  title?: string;
  small?: boolean;
  /** Which side of the button the menu lines up with. */
  align?: "left" | "right";
  /** Open above the button (for one at the bottom of a pane). */
  up?: boolean;
  disabled?: boolean;
}) {
  const { groups, label, small, align = "right" } = props;
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const title = props.title ?? "Download";

  const pick = async (item: DownloadItem) => {
    setBusy(item.key);
    try {
      await item.onPick();
      setOpen(false);
    } catch (e) {
      failed(`Couldn't download ${item.label}`)(e);
    } finally {
      setBusy(null);
    }
  };

  const shown = groups.filter((g) => g.items.length);
  const items: DropdownMenuOption[] = shown.flatMap((g, i): DropdownMenuOption[] => [
    ...(i ? [{ type: "divider" as const }] : []),
    {
      type: "section",
      id: g.title ?? String(i),
      title: g.title,
      items: g.items.map((item) => ({
        id: item.key,
        label: busy === item.key ? "Preparing…" : item.label,
        description: item.disabled ?? item.hint,
        isDisabled: !!item.disabled || busy !== null,
        // The menu closes when the download has started, not when it is asked for.
        hasCloseOnSelect: false,
        onClick: () => void pick(item),
      })),
    },
  ]);

  const size = small ? "sm" : "md";
  const icon = <IconDownload size={small ? 12 : 14} />;
  const isDisabled = props.disabled || !shown.length;
  const menu = {
    items,
    isMenuOpen: open,
    onOpenChange: setOpen,
    placement: props.up ? ("above" as const) : ("below" as const),
    alignment: align === "left" ? ("start" as const) : ("end" as const),
    menuWidth: 240,
    menuMaxHeight: 420,
  };
  return label ? (
    // The button says `label`; what it is called, and its tooltip, is the fuller `title`.
    <DropdownMenu {...menu} hasChevron={false} button={{ label: title, children: label, icon, size, isDisabled, tooltip: title === label ? undefined : title }} />
  ) : (
    <DropdownMenu
      {...menu}
      renderTrigger={(trigger) => <IconButton {...trigger} variant="ghost" size={size} label={title} tooltip={title} icon={icon} isDisabled={isDisabled} />}
    />
  );
}
