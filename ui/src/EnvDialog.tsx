import { useEffect, useState } from "react";
import * as stylex from "@stylexjs/stylex";
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
import { api, type EnvVar } from "./api";
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
});

/** The form: a new variable (`editing` null) or a change to `editing`. */
type Draft = { name: string; value: string; secret: boolean; editing: EnvVar | null };
const EMPTY: Draft = { name: "", value: "", secret: true, editing: null };

/** The project's environment variables: the API keys, tokens and settings
 * every node gets in `os.environ`. A secret is never shown again once saved. */
export function EnvDialog(props: { onClose: () => void }) {
  const { onClose } = props;
  const [vars, setVars] = useState<EnvVar[] | null>(null);
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
    api.env().then(setVars).catch((e: Error) => setErr(e.message));
  }, []);

  const act = async (label: string, fn: () => Promise<EnvVar[]>) => {
    setBusy(label);
    setErr(null);
    try {
      setVars(await fn());
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
    if (await act("save", () => api.setEnv(name, { value: draft.value, secret: draft.secret }))) reset();
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
      renderCell: (v) =>
        v.secret && !v.readable ? (
          <Badge variant="error" label="Can't be decrypted" />
        ) : v.secret ? (
          <Text type="supporting" color="secondary">
            Secret, hidden
          </Text>
        ) : (
          <Text type="code" maxLines={1} xstyle={styles.code}>
            {v.value}
          </Text>
        ),
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
    {
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
    },
  ];

  const editing = draft.editing;
  return (
    <AppDialog
      title="Environment"
      subtitle="Variables every node gets in os.environ: API keys, tokens and settings."
      width={680}
      onClose={onClose}
      purpose="form"
      onSubmit={save}
      footerNote={
        <Text type="supporting" color="secondary">
          Runs started from now on use them
        </Text>
      }
      footer={<Button label="Done" onClick={onClose} />}
    >
      <VStack gap={4}>
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
            No variables yet. A node reads one with <Text type="code">os.environ["NAME"]</Text>.
          </Text>
        )}

        <VStack gap={3}>
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
        </VStack>
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
            await act(`rm${v.name}`, () => api.removeEnv(v.name));
            if (draft.editing?.name === v.name) reset();
          }
        }}
      />
    </AppDialog>
  );
}
