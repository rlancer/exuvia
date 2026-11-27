import { useCallback, useEffect, useId, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { Code as InlineCode } from "@astryxdesign/core/Code";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { Link } from "@astryxdesign/core/Link";
import { List, ListItem } from "@astryxdesign/core/List";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { VStack } from "@astryxdesign/core/VStack";
import { dataKeysApi, hubApi, type DataKey, type HubProject } from "./api";
import { Code } from "./Code";
import { AppDialog } from "./ui/AppDialog";
import { ago } from "./util";

/** A hint under a title or beside a block to copy. */
function Note({ children }: { children: ReactNode }) {
  return (
    <Text type="supporting" color="secondary">
      {children}
    </Text>
  );
}

/** The data API's section of the docs, and the same page as Markdown for an agent. */
const DOCS = "https://exuvia.lobster.mp/docs/hub/#data-api-read-only-keys-for-your-own-apps";
const DOCS_MD = "https://exuvia.lobster.mp/docs/hub.md";

/** The data API's own OpenAPI spec on the hub at `origin`: the routes a key can use, read with no key. */
export const specUrl = (origin: string) => `${origin}/api/data/openapi.json`;

/**
 * A prompt to paste into a coding agent with a new key: where the API is,
 * how to send the key and keep it out of a commit, and to start from the
 * spec, which describes the rest.
 */
export function agentPrompt(origin: string, key: string, projects: string[]): string {
  const [first] = projects;
  return [
    "Build on data from an exuvia hub through its data API: read-only HTTP over the outputs of the projects this key reads.",
    "",
    `- Address: ${origin}/api/data/`,
    `- Key: ${key}`,
    "- Send the key as the header `Authorization: Bearer <key>`, and keep it out of code you commit: read it from an environment variable such as EXUVIA_DATA_KEY, set in a .env file that git ignores.",
    `- Projects it reads: ${projects.join(", ")}. In SQL each project is a catalog of its own: "${first}".<node>.`,
    "",
    `1. Fetch ${specUrl(origin)} (no key needed). It describes every route, its answers and its errors: follow it.`,
    `2. GET ${origin}/api/data/projects with the key: each project's nodes, with their columns and row counts.`,
    "3. Read a node's output, or POST one SELECT to /api/data/query (it can join projects). For typed columns and every row, send `Accept: application/vnd.apache.arrow.stream`.",
    "",
    `The data API's docs, as Markdown: ${DOCS_MD} (the section "Data API: read-only keys for your own apps").`,
  ].join("\n");
}

type Try = "agent" | "curl";
const TRY: { id: Try; label: string; hint: string }[] = [
  { id: "agent", label: "Ask your agent", hint: "Paste this into Claude Code, Cursor, Codex or any coding agent: it reads the spec and builds from there." },
  { id: "curl", label: "curl", hint: "Run this in a terminal to see what the key reads." },
];

/** What an app sends with a new key: one query across the projects it reads. */
export function curlExample(origin: string, key: string, projects: string[]): string {
  const [first] = projects;
  const sql = `select * from "${first}".<node> limit 10`;
  return [
    `curl -s ${origin}/api/data/projects -H "Authorization: Bearer ${key}"`,
    "",
    `curl -s ${origin}/api/data/query \\`,
    `  -H "Authorization: Bearer ${key}" -H "Content-Type: application/json" \\`,
    `  -d '${JSON.stringify({ sql })}'`,
  ].join("\n");
}

/**
 * Read-only keys for the data API: for apps built on this hub's data that
 * need more than its views. Each key reads the projects ticked for it, never
 * more than its maker can open, and is shown once, when it is made.
 */
export function DataKeysDialog(props: { onClose: () => void }) {
  const [keys, setKeys] = useState<DataKey[] | null>(null);
  const [projects, setProjects] = useState<HubProject[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [made, setMade] = useState<(DataKey & { key: string }) | null>(null);
  // The key being edited (its id), or null for a new one.
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [tryWith, setTryWith] = useState<Try>("agent");
  const panel = useId();
  const current = TRY.find((t) => t.id === tryWith)!;

  const load = useCallback(() => dataKeysApi.list().then(setKeys).catch((e: Error) => setErr(e.message)), []);
  useEffect(() => {
    load();
    hubApi
      .projects()
      .then((l) => setProjects(l.projects))
      .catch((e: Error) => setErr(e.message));
  }, [load]);

  const reset = () => {
    setEditing(null);
    setName("");
    setChosen(new Set());
  };
  const toggle = (id: string, on: boolean) =>
    setChosen((s) => {
      const next = new Set(s);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  const edit = (k: DataKey) => {
    setMade(null);
    setEditing(k.id);
    setName(k.name);
    setChosen(new Set(k.projects));
  };
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || !chosen.size) return;
    setErr(null);
    setBusy(true);
    try {
      const list = [...chosen];
      if (editing) await dataKeysApi.update(editing, { name: name.trim(), projects: list });
      else setMade(await dataKeysApi.create(name.trim(), list));
      reset();
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const revoke = async (k: DataKey) => {
    setErr(null);
    try {
      await dataKeysApi.revoke(k.id);
      if (made?.id === k.id) setMade(null);
      if (editing === k.id) reset();
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <AppDialog
      title="Data API keys"
      subtitle="Read-only access to chosen projects' outputs, for apps of your own."
      width={640}
      onClose={props.onClose}
      footer={<Button label="Done" onClick={props.onClose} />}
    >
      <VStack gap={5}>
        <Text color="secondary">
          An app with a key lists its projects' tables, reads any node's output and runs one SQL query across all of
          its projects at once, each project a schema of its own (<InlineCode>"sales".orders</InlineCode>), as JSON or
          Arrow. It can't change anything, and it never reads more than you can open.
        </Text>
        <HStack gap={4} wrap="wrap">
          <Link isExternalLink href={DOCS}>
            Data API docs
          </Link>
          <Link isExternalLink href={specUrl(location.origin)}>
            OpenAPI spec
          </Link>
        </HStack>
        {err && <Banner role="alert" status="error" title={err} />}
        {made && (
          <VStack gap={2}>
            <Heading level={3}>Your new key</Heading>
            <Code text={made.key} />
            <Note>
              <InlineCode>{made.name}</InlineCode> reads {made.projects.join(", ")}. This is the only time it is shown: keep it out of
              code you commit, and make another if you lose it.
            </Note>
            <TabList role="tablist" size="sm" hasDivider value={tryWith} onChange={(t) => setTryWith(t as Try)}>
              {TRY.map((t) => (
                <Tab key={t.id} value={t.id} label={t.label} panelId={panel} />
              ))}
            </TabList>
            <VStack gap={2} id={panel} role="tabpanel" aria-label={current.label}>
              {tryWith === "agent" ? (
                <Code text={agentPrompt(location.origin, made.key, made.projects)} wrap />
              ) : (
                <Code text={curlExample(location.origin, made.key, made.projects)} />
              )}
              <Note>
                {current.hint} The key is in it: keep it to yourself, and don't commit it.
              </Note>
            </VStack>
          </VStack>
        )}
        <form onSubmit={save}>
          <VStack gap={3}>
            <Heading level={3}>{editing ? "Change the key" : "Make a key"}</Heading>
            <TextInput
              label="What the key is for"
              placeholder="e.g. sales dashboard"
              value={name}
              onChange={(v) => setName(v.slice(0, 80))}
            />
            <VStack gap={1}>
              <Text weight="semibold">Projects it reads</Text>
              {projects === null ? (
                <Center padding={2}>
                  <Spinner label="Loading projects…" />
                </Center>
              ) : projects.length === 0 ? (
                <Note>No projects yet.</Note>
              ) : (
                <div {...stylex.props(styles.projects)}>
                  {projects.map((p) => (
                    <CheckboxInput
                      key={p.id}
                      label={p.name && p.name !== p.id ? `${p.name} (${p.id})` : p.id}
                      value={chosen.has(p.id)}
                      onChange={(on) => toggle(p.id, on)}
                    />
                  ))}
                </div>
              )}
            </VStack>
            <HStack gap={2} hAlign="end">
              {editing && <Button variant="ghost" label="Cancel" isDisabled={busy} onClick={reset} />}
              <Button
                type="submit"
                variant="primary"
                label={editing ? "Save" : "Create key"}
                isDisabled={busy || !chosen.size}
                tooltip={chosen.size ? undefined : "Tick at least one project"}
              />
            </HStack>
          </VStack>
        </form>
        {keys === null && !err ? (
          <Center padding={2}>
            <Spinner label="Loading…" />
          </Center>
        ) : (
          !!keys?.length && (
            <VStack gap={2}>
              <Heading level={3}>Your keys</Heading>
              <List hasDividers edge="flush">
                {keys.map((k) => (
                  <ListItem
                    key={k.id}
                    label={
                      <HStack gap={2} vAlign="center">
                        <Text maxLines={1}>{k.name}</Text>
                        <Text type="code" color="secondary">
                          …{k.hint}
                        </Text>
                      </HStack>
                    }
                    description={`reads ${k.projects.join(", ") || "nothing"} · ${k.last_used ? `used ${ago(k.last_used)}` : "never used"}`}
                    endContent={
                      <HStack gap={1}>
                        <Button variant="ghost" size="sm" label="Edit" tooltip={`Rename ${k.name} or change its projects`} onClick={() => edit(k)} />
                        <Button
                          variant="ghost"
                          size="sm"
                          label="Revoke"
                          tooltip={`Revoke ${k.name}: apps using it are turned away`}
                          onClick={() => revoke(k)}
                        />
                      </HStack>
                    }
                  />
                ))}
              </List>
            </VStack>
          )
        )}
      </VStack>
    </AppDialog>
  );
}

const styles = stylex.create({
  projects: { maxHeight: 220, overflowY: "auto", display: "flex", flexDirection: "column", gap: spacingVars["--spacing-1"] },
});
