import { useEffect, useMemo, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { EditorState } from "@uiw/react-codemirror";
import { unifiedMergeView } from "@codemirror/merge";
import { colorVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { List, ListItem } from "@astryxdesign/core/List";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { VStack } from "@astryxdesign/core/VStack";
import { api, type CodeTag, type TagFile } from "./api";
import { tagNameProblem, tagStatusLabel } from "./tags";
import { language } from "./lang";
import { ago } from "./util";
import { useAccess } from "./Access";
import { IconPlus } from "./icons";
import { AppDialog } from "./ui/AppDialog";
import { CodeEditor } from "./ui/CodeEditor";

const when = (ts: number) => new Date(ts * 1000).toLocaleString();

/**
 * The Tags list of the side panel's History tab: every name given to the
 * whole project's code at one save, newest first, and the button that tags
 * the code as it is now. Picking one opens it next to the graph.
 */
export function TagRail(props: {
  /** Changes when the code or the tags change: the list is read again. */
  tick: unknown;
  /** The tag open in the main area, highlighted. */
  current: string | null;
  onOpen: (name: string) => void;
}) {
  const { canEdit } = useAccess();
  const [tags, setTags] = useState<CodeTag[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [making, setMaking] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    api.codeTags().then(
      (t) => live && (setTags(t), setErr(null)),
      (e: Error) => live && setErr(e.message),
    );
    return () => {
      live = false;
    };
  }, [props.tick, reload]);

  return (
    <>
      {canEdit && (
        <div {...stylex.props(styles.bar)}>
          <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.grow}>
            A tag names the whole project's code at one save.
          </Text>
          <Button size="sm" label="New tag" icon={<IconPlus />} tooltip="Tag the code as it is now" onClick={() => setMaking(true)} xstyle={styles.fixed} />
        </div>
      )}
      {err && <Banner status="error" container="section" title={err} />}
      <div {...stylex.props(styles.scroll)}>
        <List density="compact" hasDividers aria-label="Tags">
          {(tags ?? []).map((t) => (
            <ListItem
              key={t.name}
              isSelected={t.name === props.current}
              onClick={() => props.onOpen(t.name)}
              label={
                <span title={t.message ?? t.name}>
                  <Text type="code" maxLines={1} hasTruncateTooltip={false}>
                    {t.name}
                  </Text>
                </span>
              }
              description={
                <span title={when(t.created_at)}>
                  <Text type="supporting" color="secondary" maxLines={1}>
                    {[t.message, ago(t.created_at), t.author].filter(Boolean).join(" · ")}
                  </Text>
                </span>
              }
              endContent={
                t.changed_since ? (
                  <span title={`${t.changed_since} file${t.changed_since === 1 ? "" : "s"} differ from the code now`}>
                    <Badge label={String(t.changed_since)} />
                  </span>
                ) : undefined
              }
            />
          ))}
        </List>
        {tags && !tags.length && (
          <Note>
            No tags yet. Tag the code before a big change, or for the version behind numbers you shared, to find and compare it
            by name later.
          </Note>
        )}
        {!tags && !err && <Note>Loading…</Note>}
      </div>
      {making && (
        <NewTagDialog
          onClose={() => setMaking(false)}
          onCreated={(t) => {
            setMaking(false);
            setReload((n) => n + 1);
            props.onOpen(t.name);
          }}
        />
      )}
    </>
  );
}

/** Name the code as it is now: a short name and, optionally, what it is. */
export function NewTagDialog(props: { onClose: () => void; onCreated: (tag: CodeTag) => void }) {
  const [name, setName] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const problem = tagNameProblem(name);

  const save = async () => {
    if (problem) return;
    setBusy(true);
    setErr(null);
    try {
      props.onCreated(await api.createTag(name.trim(), message.trim()));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AppDialog
      title="Tag the code"
      subtitle="Names every node, view and file as it is now, so this version can be found and compared later."
      onClose={props.onClose}
      onSubmit={save}
      footer={(formId) => (
        <>
          <Button label="Cancel" onClick={props.onClose} />
          <Button type="submit" form={formId} variant="primary" label="Tag" isDisabled={busy || !!problem} isLoading={busy} />
        </>
      )}
    >
      <VStack gap={4}>
        {err && <Banner role="alert" status="error" title={err} />}
        <TextInput
          label="Name"
          hasAutoFocus
          autoComplete="off"
          placeholder="v1, q3-review, before-refactor"
          value={name}
          onChange={setName}
          description={name && problem ? problem : "Letters, digits, '.', '_' and '-'. A name always means the version it was given to."}
        />
        <TextArea label="Message" isOptional rows={3} maxLength={2000} value={message} onChange={setMessage} placeholder="What this version is" />
      </VStack>
    </AppDialog>
  );
}

/** What the file list shows: the files that differ from now, or every file. */
type Which = "changed" | "all";

/**
 * One tag, open in the main area: the files it holds beside the files now,
 * and the picked file's diff from its version at the tag to its text now. A
 * node's or a view file's version at the tag can be restored; the tag itself
 * can be deleted (the code it named stays in the history).
 */
export function TagBrowser(props: {
  name: string;
  /** Changes when the code changes: the comparison is read again. */
  tick: unknown;
  dark: boolean;
  onRestored?: () => void;
  onDeleted?: () => void;
}) {
  const { name, dark } = props;
  const { canEdit } = useAccess();
  const [tag, setTag] = useState<Awaited<ReturnType<typeof api.codeTag>> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [which, setWhich] = useState<Which>("changed");
  const [selPath, setSelPath] = useState<string | null>(null);
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState<"delete" | TagFile | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    setErr(null);
    api.codeTag(name).then(
      (t) => live && setTag(t),
      (e: Error) => live && (setTag(null), setErr(e.message)),
    );
    return () => {
      live = false;
    };
  }, [name, props.tick, reload]);

  const files = useMemo(() => tag?.files ?? [], [tag]);
  const changed = useMemo(() => files.filter((f) => f.status !== "same"), [files]);
  const shown = which === "changed" ? changed : files;
  const sel = shown.find((f) => f.path === selPath) ?? shown[0] ?? null;

  useEffect(() => {
    for (const h of [sel?.hash, sel?.current_hash]) {
      if (!h || h in texts) continue;
      api.codeBlob(h).then(
        (t) => setTexts((x) => ({ ...x, [h]: t })),
        (e: Error) => setErr(e.message),
      );
    }
  }, [sel?.hash, sel?.current_hash, texts]);

  // The diff reads from the tag's version to the file now.
  const original = sel?.hash ? texts[sel.hash] : "";
  const doc = sel?.current_hash ? texts[sel.current_hash] : "";
  const ready = sel != null && original !== undefined && doc !== undefined;
  const target = sel?.hash ? restoreTargetOf(sel) : null;

  const act = async () => {
    const what = confirm;
    if (!what) return;
    setBusy(true);
    setErr(null);
    try {
      if (what === "delete") {
        await api.deleteTag(name);
        setConfirm(null);
        props.onDeleted?.();
        return;
      }
      const t = restoreTargetOf(what);
      if (!t || !what.hash) return;
      if (t.kind === "node") await api.restoreNode(t.name, what.hash);
      else await api.restoreViewFile(t.name, t.path, what.hash);
      setConfirm(null);
      setReload((n) => n + 1);
      props.onRestored?.();
    } catch (e) {
      setConfirm(null);
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!tag)
    return (
      <div {...stylex.props(styles.frame, styles.alone)}>
        {err ? <Banner status="error" container="section" title={err} /> : <Note>Loading tag…</Note>}
      </div>
    );

  return (
    <div {...stylex.props(styles.frame, styles.alone)}>
      <div {...stylex.props(styles.bar)}>
        <span title={when(tag.created_at)} {...stylex.props(styles.grow)}>
          <Text type="supporting" color="secondary" maxLines={1}>
            {[tag.message, `tagged ${ago(tag.created_at)}`, tag.author && `by ${tag.author}`].filter(Boolean).join(" · ")}
          </Text>
        </span>
        {canEdit && <Button size="sm" variant="destructive" label="Delete tag" tooltip="The code it names stays in the history" onClick={() => setConfirm("delete")} />}
      </div>
      <div {...stylex.props(styles.history)}>
        <div {...stylex.props(styles.list)}>
          <div {...stylex.props(styles.bar)}>
            <SegmentedControl size="sm" layout="fill" label="Which files" value={which} onChange={(v) => setWhich(v === "all" ? "all" : "changed")} xstyle={styles.grow}>
              <SegmentedControlItem value="changed" label={`Changed since (${changed.length})`} />
              <SegmentedControlItem value="all" label={`All (${files.length})`} />
            </SegmentedControl>
          </div>
          <div {...stylex.props(styles.scroll)}>
            <List density="compact" hasDividers aria-label="Files in the tag">
              {shown.map((f) => (
                <ListItem
                  key={f.path}
                  isSelected={f.path === sel?.path}
                  onClick={() => setSelPath(f.path)}
                  label={
                    <Text type="code" maxLines={1} hasTruncateTooltip={false}>
                      {f.path}
                    </Text>
                  }
                  endContent={f.status === "same" ? undefined : <Badge variant={f.status === "deleted" ? "error" : "neutral"} label={tagStatusLabel[f.status]} />}
                />
              ))}
            </List>
            {!shown.length && <Note>The code now is the same as at this tag.</Note>}
          </div>
        </div>
        <div {...stylex.props(styles.diff)}>
          {err && <Banner status="error" container="section" title={err} />}
          {sel && (
            <div {...stylex.props(styles.bar)}>
              <Text type="supporting" color="secondary" maxLines={1} xstyle={styles.grow}>
                {sel.status === "added"
                  ? "new since the tag: the tag has no such file"
                  : sel.status === "deleted"
                    ? "deleted since the tag: this is its text at the tag"
                    : sel.status === "same"
                      ? "the same now as at the tag"
                      : "what changed from the tag to now"}
              </Text>
              {canEdit && target && sel.status !== "same" && (
                <Button
                  size="sm"
                  label={sel.status === "deleted" ? (target.kind === "node" ? "Restore node" : "Restore file") : "Restore this version"}
                  tooltip="Make the tag's version of this file current again"
                  isDisabled={busy}
                  onClick={() => setConfirm(sel)}
                />
              )}
            </div>
          )}
          {sel && ready ? (
            <CodeEditor
              key={`${name}:${sel.path}:${sel.hash}:${sel.current_hash}`}
              value={sel.status === "deleted" ? original : doc}
              editable={false}
              activeLine={false}
              dark={dark}
              extensions={[
                ...language(sel.path),
                EditorState.readOnly.of(true),
                ...(sel.status === "changed" ? [unifiedMergeView({ original, mergeControls: false, collapseUnchanged: { margin: 3, minSize: 6 } })] : []),
              ]}
            />
          ) : sel ? (
            <Note>Loading…</Note>
          ) : (
            <div {...stylex.props(styles.note)}>
              <EmptyState isCompact title="Nothing has changed" description="Every file is as it was when this tag was made." />
            </div>
          )}
        </div>
      </div>
      {confirm && (
        <AlertDialog
          isOpen
          onOpenChange={(open) => {
            if (!open && !busy) setConfirm(null);
          }}
          title={confirm === "delete" ? `Delete tag ${name}?` : `Restore ${confirm.path} as tagged ${name}?`}
          description={
            confirm === "delete"
              ? "The tag goes; the code it named stays in the history, and a git mirror keeps the git tag it already made."
              : `This makes the file's version at tag ${name} the current code${confirm.status === "deleted" ? ", bringing the deleted file back" : ""}. The code there now stays in the history, so this can be undone.`
          }
          actionLabel={confirm === "delete" ? "Delete" : "Restore"}
          actionVariant={confirm === "delete" ? "destructive" : "primary"}
          isActionLoading={busy}
          onAction={act}
        />
      )}
    </div>
  );
}

function restoreTargetOf(f: TagFile): { kind: "node"; name: string } | { kind: "view"; name: string; path: string } | null {
  if (f.node) return { kind: "node", name: f.node };
  const prefix = `views/${f.view}/`;
  if (f.view && f.path.startsWith(prefix)) return { kind: "view", name: f.view, path: f.path.slice(prefix.length) };
  return null;
}

function Note({ children }: { children: ReactNode }) {
  return (
    <div {...stylex.props(styles.note)}>
      <Text color="secondary" justify="center">
        {children}
      </Text>
    </div>
  );
}

const COMPACT = "@media (max-width: 900px)";

const styles = stylex.create({
  frame: { flexGrow: 1, minWidth: 0, minHeight: 0, height: "100%" },
  alone: { display: "flex", flexDirection: "column", backgroundColor: colorVars["--color-background-surface"] },
  history: {
    display: "grid",
    flexGrow: 1,
    minHeight: 0,
    gap: "1px",
    backgroundColor: colorVars["--color-border"],
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
    backgroundColor: colorVars["--color-background-surface"],
  },
  grow: { flexGrow: 1, minWidth: 0 },
  // The button keeps its words; the hint beside it is what gets cut.
  fixed: { flexShrink: 0 },
  scroll: { flexGrow: 1, minHeight: 0, overflowY: "auto" },
  note: { display: "grid", placeItems: "center", flexGrow: 1, minHeight: 0, padding: spacingVars["--spacing-6"] },
});
