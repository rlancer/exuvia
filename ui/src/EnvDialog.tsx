import { useEffect, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars } from "@astryxdesign/core/theme/tokens.stylex";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { FileInput } from "@astryxdesign/core/FileInput";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { pixel, proportional, Table, type TableColumn } from "@astryxdesign/core/Table";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { VStack } from "@astryxdesign/core/VStack";
import { Heading } from "@astryxdesign/core/Heading";
import { Link } from "@astryxdesign/core/Link";
import { api, type EnvRequest, type EnvState, type EnvVar, type GroupRef, type InheritedVar } from "./api";
import { IconTrash } from "./icons";
import { AppDialog } from "./ui/AppDialog";
import { ago } from "./util";

/** What the server accepts as a name (exuvia/envvars.py NAME). */
const NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
/** The longest value the server keeps (exuvia/envvars.py MAX_VALUE_CHARS). */
const MAX_VALUE = 32_768;

const styles = stylex.create({
  grow: { flexGrow: 1, flexBasis: 0, minWidth: 0 },
  // A key or a name is read character by character.
  code: { fontVariantLigatures: "none", fontFeatureSettings: '"liga" 0, "calt" 0' },
  // A group's variable the project's own replaces.
  overridden: { textDecorationLine: "line-through", color: colorVars["--color-text-secondary"] },
});

/** Who asked for a secret: the built-in agent, an MCP client, the API. */
const asker = (origin: string) =>
  origin === "agent" ? "The agent" : origin === "mcp" ? "Your coding agent" : origin === "api" ? "An API client" : origin;

/** The form: a new variable (`editing` null) or a change to `editing`. */
type Draft = { name: string; value: string; secret: boolean; editing: EnvVar | null };
const EMPTY: Draft = { name: "", value: "", secret: true, editing: null };

/** Where an editor's variables live: a project's (`api`) or a hub group's. */
export type EnvClient = {
  load: () => Promise<EnvState>;
  set: (name: string, b: { value?: string; secret: boolean }) => Promise<EnvState>;
  remove: (name: string) => Promise<EnvState>;
  dismiss?: (name: string) => Promise<EnvState>;
};

const projectEnv: EnvClient = {
  load: api.env,
  set: api.setEnv,
  remove: api.removeEnv,
  dismiss: api.dismissEnvRequest,
};

/** The project's environment variables: the API keys, tokens and settings
 * every node gets in `os.environ`. A secret is never shown again once saved.
 * On a hub, what the project's group and the hub give it are listed too, read-only. */
export function EnvDialog(props: {
  onClose: () => void;
  /** A variable to start on (a link to `#env=<name>`): changed if it exists, otherwise added. */
  initialName?: string;
  /** Called with each new state, so the bar's count of requests follows. */
  onChange?: (s: EnvState) => void;
}) {
  const [state, setState] = useState<EnvState | null>(null);
  return (
    <AppDialog
      title="Environment"
      subtitle="Variables every node gets in os.environ: API keys, tokens and settings."
      width={680}
      onClose={props.onClose}
      purpose="form"
      footerNote={
        <Text type="supporting" color="secondary">
          Runs started from now on use them
        </Text>
      }
      footer={<Button label="Done" onClick={props.onClose} />}
    >
      <EnvEditor
        client={projectEnv}
        initialName={props.initialName}
        onChange={(s) => {
          setState(s);
          props.onChange?.(s);
        }}
        below={
          state && (state.group || state.inherited?.length) ? (
            <Inherited group={state.group ?? null} vars={state.inherited ?? []} own={state.variables} />
          ) : undefined
        }
      />
    </AppDialog>
  );
}

/** What the project's group and the hub give it, each under its own heading. */
function Inherited(props: { group: GroupRef | null; vars: InheritedVar[]; own: EnvVar[] }) {
  const { group, vars } = props;
  const own = new Set(props.own.map((v) => v.name));
  const fromHub = vars.filter((v) => v.source === "hub");
  return (
    <VStack gap={6}>
      {group && (
        <Layer
          title={`From group ${group.name}`}
          link={{ href: `/?group=${encodeURIComponent(group.id)}`, label: "Open the group" }}
          description="Every run gets these too, unless the project sets the same name itself. They are changed on the group's page, by its editors."
          vars={vars.filter((v) => v.source === "group")}
          empty="The group has no variables yet."
          overriddenBy={() => "Overridden here"}
        />
      )}
      {fromHub.length > 0 && (
        <Layer
          title="From the hub"
          link={{ href: "/?env=hub", label: "Open the hub's variables" }}
          description={`Every project on the hub gets these, unless it${group ? " or its group" : ""} sets the same name. Only hub admins change them.`}
          vars={fromHub}
          empty=""
          overriddenBy={(v) => (own.has(v.name) ? "Overridden here" : "Overridden by the group")}
        />
      )}
    </VStack>
  );
}

/** One layer of inherited variables: names, plain values, and which of them a closer layer overrides. */
function Layer(props: {
  title: string;
  link: { href: string; label: string };
  description: string;
  vars: InheritedVar[];
  empty: string;
  overriddenBy: (v: InheritedVar) => string;
}) {
  const { vars } = props;
  const columns: TableColumn<InheritedVar>[] = [
    {
      key: "name",
      header: "Name",
      width: proportional(2, { minWidth: 140 }),
      renderCell: (v) => (
        <Text type="code" xstyle={[styles.code, v.overridden && styles.overridden]}>
          {v.name}
        </Text>
      ),
    },
    { key: "value", header: "Value", width: proportional(2, { minWidth: 120 }), renderCell: (v) => <ValueCell v={v} /> },
    {
      key: "overridden",
      header: "",
      width: proportional(1.5, { minWidth: 100 }),
      renderCell: (v) =>
        v.overridden ? (
          <Text type="supporting" color="secondary" maxLines={1}>
            {props.overriddenBy(v)}
          </Text>
        ) : null,
    },
  ];
  return (
    <VStack gap={2}>
      <HStack gap={2} vAlign="center" wrap="wrap">
        <Heading level={3}>{props.title}</Heading>
        <Link href={props.link.href} type="supporting">
          {props.link.label}
        </Link>
      </HStack>
      <Text type="supporting" color="secondary">
        {props.description}
      </Text>
      {vars.length ? (
        <Table<InheritedVar> data={vars} columns={columns} idKey="name" density="compact" />
      ) : (
        <Text type="supporting" color="secondary">
          {props.empty}
        </Text>
      )}
    </VStack>
  );
}

/** A variable's value cell: a plain value, or what is known of a secret. */
function ValueCell(props: { v: EnvVar }) {
  const { v } = props;
  return v.secret && !v.readable ? (
    <Badge variant="error" label="Can't be decrypted" />
  ) : v.secret ? (
    <Text type="supporting" color="secondary">
      Secret, hidden
    </Text>
  ) : (
    <Text type="code" maxLines={1} xstyle={styles.code}>
      {v.value}
    </Text>
  );
}

/**
 * A list of variables and the form that adds or changes one: a project's, in
 * the Environment dialog, or a hub group's, on its page. Read-only (`canEdit`
 * false) it lists them only.
 */
export function EnvEditor(props: {
  client: EnvClient;
  /** A variable to start on (a link to `#env=<name>`): changed if it exists, otherwise added. */
  initialName?: string;
  /** Called with each new state, so the bar's count of requests follows. */
  onChange?: (s: EnvState) => void;
  canEdit?: boolean;
  /** What the list says when there is nothing in it. */
  empty?: string;
  /** Shown under the list, above the form (a project's inherited variables). */
  below?: ReactNode;
}) {
  const { client, initialName, onChange } = props;
  const canEdit = props.canEdit ?? true;
  const [vars, setVarsOnly] = useState<EnvVar[] | null>(null);
  const [requests, setRequests] = useState<EnvRequest[]>([]);
  const setState = (s: EnvState) => {
    setVarsOnly(s.variables);
    setRequests(s.requests);
    onChange?.(s);
  };
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [removing, setRemoving] = useState<EnvVar | null>(null);
  // A key file read into the value (a .pem), shown in the file picker.
  const [file, setFile] = useState<File | null>(null);
  const reset = () => {
    setErr(null);
    setFile(null);
    setDraft(EMPTY);
  };

  const loadFile = async (picked: File | File[] | null) => {
    const f = Array.isArray(picked) ? (picked[0] ?? null) : picked;
    setFile(f);
    if (!f) return;
    const text = await f.text();
    if (text.includes("\u0000") || text.includes("\uFFFD")) {
      setErr(`${f.name} is not a text file. A binary key goes in as base64 text.`);
      setFile(null);
      return;
    }
    setErr(null);
    setDraft((d) => ({ ...d, value: text }));
  };

  useEffect(() => {
    client
      .load()
      .then((s) => {
        setState(s);
        const v = initialName ? s.variables.find((x) => x.name === initialName) : undefined;
        if (v) setDraft({ name: v.name, value: v.secret ? "" : (v.value ?? ""), secret: v.secret, editing: v });
        else if (initialName) setDraft({ ...EMPTY, name: initialName });
      })
      .catch((e: Error) => setErr(e.message));
  }, []);

  const act = async (label: string, fn: () => Promise<EnvState>) => {
    setBusy(label);
    setErr(null);
    try {
      setState(await fn());
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(null);
    }
  };

  const lost = vars?.filter((v) => !v.readable) ?? [];
  const name = draft.editing?.name ?? draft.name.trim();
  const exists = !draft.editing && !!vars?.some((v) => v.name === name);
  const nameProblem = !draft.editing && draft.name.trim() && !NAME.test(draft.name.trim())
    ? "Letters, digits and underscores, starting with a letter or underscore."
    : exists
      ? `${name} is already set: pick it in the list to change it.`
      : null;
  // A plain variable's value is filled in to change; a secret's is replaced whole.
  const canSave = !busy && !!name && !nameProblem && draft.value !== "";

  const save = async () => {
    if (!canSave) return;
    if (await act("save", () => client.set(name, { value: draft.value, secret: draft.secret }))) reset();
  };

  const columns: TableColumn<EnvVar>[] = [
    {
      key: "name",
      header: "Name",
      width: proportional(2, { minWidth: 140 }),
      renderCell: (v) => (
        <Text type="code" xstyle={styles.code}>
          {v.name}
        </Text>
      ),
    },
    {
      key: "value",
      header: "Value",
      width: proportional(2, { minWidth: 120 }),
      renderCell: (v) => <ValueCell v={v} />,
    },
    {
      key: "updated_at",
      header: "Updated",
      width: proportional(1.5, { minWidth: 100 }),
      renderCell: (v) => (
        <Text type="supporting" color="secondary" maxLines={1}>
          {[ago(v.updated_at), v.updated_by && `by ${v.updated_by}`].filter(Boolean).join(" ")}
        </Text>
      ),
    },
    ...(!canEdit ? [] : [{
      key: "actions",
      header: "",
      width: pixel(136),
      align: "end",
      renderCell: (v) => (
        <HStack gap={1} hAlign="end">
          <Button
            variant="ghost"
            size="sm"
            label="Change"
            tooltip={v.secret ? `Replace ${v.name}'s value` : `Change ${v.name}`}
            isDisabled={!!busy}
            onClick={() => {
              setErr(null);
              setFile(null);
              setDraft({ name: v.name, value: v.secret ? "" : (v.value ?? ""), secret: v.secret, editing: v });
            }}
          />
          <IconButton
            variant="ghost"
            size="sm"
            label={`Remove ${v.name}`}
            tooltip={`Remove ${v.name}`}
            icon={<IconTrash />}
            isDisabled={!!busy}
            isLoading={busy === `rm${v.name}`}
            onClick={() => setRemoving(v)}
          />
        </HStack>
      ),
    } satisfies TableColumn<EnvVar>]),
  ];

  const editing = draft.editing;
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <VStack gap={4}>
        {requests.map((r) => (
          <Banner
            key={r.name}
            status="info"
            title={`${asker(r.origin)} asks you to set ${r.name}`}
            description={r.reason ? `${r.reason}. You enter the value here; it never passes through the agent.` : "You enter the value here; it never passes through the agent."}
            endContent={
              <HStack gap={1}>
                <Button size="sm" label="Set it" isDisabled={!!busy} onClick={() => { setErr(null); setFile(null); setDraft({ ...EMPTY, name: r.name }); }} />
                {client.dismiss && (
                  <Button variant="ghost" size="sm" label="Dismiss" isDisabled={!!busy} onClick={() => act(`dismiss${r.name}`, () => client.dismiss!(r.name))} />
                )}
              </HStack>
            }
          />
        ))}
        {!!lost.length && (
          <Banner
            status="warning"
            title={`${lost.map((v) => v.name).join(", ")} can't be decrypted`}
            description={
              lost.length === 1
                ? "This server doesn't have the key it was saved with, so runs don't get it. Set EXUVIA_SECRETS_KEY on the server to that key, or enter it again."
                : "This server doesn't have the key they were saved with, so runs don't get them. Set EXUVIA_SECRETS_KEY on the server to that key, or enter them again."
            }
          />
        )}
        {vars && vars.length > 0 && <Table<EnvVar> data={vars} columns={columns} idKey="name" density="compact" />}
        {vars && vars.length === 0 && (
          <Text type="supporting" color="secondary">
            {props.empty ?? (
              <>
                No variables yet. A node reads one with <Text type="code">os.environ["NAME"]</Text>.
              </>
            )}
          </Text>
        )}
        {!canEdit && err && <Banner status="error" title={err} />}
        {props.below}

        {canEdit && <VStack gap={3}>
          <Text weight="medium">{editing ? `Change ${editing.name}` : "Add a variable"}</Text>
          {!editing && (
            <TextInput
              label="Name"
              value={draft.name}
              placeholder="FRED_API_KEY"
              hasSpellCheck={false}
              autoCapitalize="characters"
              autoComplete="off"
              onChange={(name) => setDraft((d) => ({ ...d, name }))}
              status={nameProblem ? { type: "error", message: nameProblem } : undefined}
              controlXstyle={styles.code}
            />
          )}
          <TextArea
            label="Value"
            description={
              editing?.secret
                ? "The saved value stays hidden. Paste or load a new one to replace it."
                : "Several lines, such as a PEM key, are kept as they are."
            }
            value={draft.value}
            rows={3}
            hasSpellCheck={false}
            htmlMaxLength={MAX_VALUE}
            onChange={(value) => setDraft((d) => ({ ...d, value }))}
          />
          <FileInput
            label="Or load it from a file"
            description="A key or certificate file (.pem, .key, .json): its text becomes the value."
            placeholder="Choose a file"
            value={file}
            maxSize={MAX_VALUE}
            onChange={(f) => void loadFile(f)}
          />
          <CheckboxInput
            label="Secret"
            description="Never shown again once saved, to you, other people or the agent, and masked as *** in run logs."
            value={draft.secret}
            onChange={(secret) => setDraft((d) => ({ ...d, secret }))}
          />
          {err && (
            <Banner status="error" title={err} />
          )}
          <HStack gap={2} hAlign="end">
            {editing && <Button variant="ghost" label="Cancel" isDisabled={!!busy} onClick={reset} />}
            <Button type="submit" variant="primary" label={editing ? "Save" : "Add"} isDisabled={!canSave} isLoading={busy === "save"} />
          </HStack>
        </VStack>}
      </VStack>
      <AlertDialog
        isOpen={!!removing}
        onOpenChange={(open) => {
          if (!open) setRemoving(null);
        }}
        title={`Remove ${removing?.name ?? "this variable"}?`}
        description="Nodes that read it fail on their next run until it is set again. A secret's value cannot be recovered."
        actionLabel="Remove"
        actionVariant="destructive"
        onAction={async () => {
          const v = removing;
          setRemoving(null);
          if (v) {
            await act(`rm${v.name}`, () => client.remove(v.name));
            if (draft.editing?.name === v.name) reset();
          }
        }}
      />
    </form>
  );
}
