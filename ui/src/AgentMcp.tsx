import { useCallback, useEffect, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { Code } from "@astryxdesign/core/Code";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack } from "@astryxdesign/core/HStack";
import { IconButton } from "@astryxdesign/core/IconButton";
import { List, ListItem } from "@astryxdesign/core/List";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Switch } from "@astryxdesign/core/Switch";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { VStack } from "@astryxdesign/core/VStack";
import { agentMcpApi, type AgentMcpList, type AgentMcpServer, type AgentMcpTest } from "./api";
import { IconPlus, IconTrash } from "./icons";
import { AppDialog } from "./ui/AppDialog";
import { statusText } from "./ui/state";

const NAME = /^[a-z][a-z0-9_]{0,23}$/;
const PORTKEY_URL = "https://mcp.portkey.ai/";
const PORTKEY_HEADER = "x-portkey-api-key";

/** A header row in the form: one the server has (value left empty keeps it) or a new one. */
interface HeaderRow {
  key: number;
  name: string;
  value: string;
  /** Set for a header the server already has: its value's last 4 characters. */
  hint?: string;
  removed?: boolean;
}

/**
 * The MCP servers the built-in agent connects to when you talk to it: Linear,
 * GitHub, your own, or any of them behind Portkey's MCP gateway. Each has a
 * URL and the headers that sign it in; the values go to the server and never
 * come back, so a row shows a header's name and the end of its value.
 */
export function AgentMcpDialog(props: { onClose: () => void }) {
  const [data, setData] = useState<AgentMcpList | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState<AgentMcpServer | "new" | null>(null);
  const [tests, setTests] = useState<Record<string, AgentMcpTest | "running">>({});
  const [removing, setRemoving] = useState<AgentMcpServer | null>(null);

  const load = useCallback(
    () =>
      agentMcpApi
        .list()
        .then(setData)
        .catch((e: Error) => setErr(e.message)),
    [],
  );
  useEffect(() => {
    load();
  }, [load]);

  const test = async (name: string) => {
    setTests((t) => ({ ...t, [name]: "running" }));
    try {
      const r = await agentMcpApi.test(name);
      setTests((t) => ({ ...t, [name]: r }));
    } catch (e) {
      setTests((t) => ({ ...t, [name]: { ok: false, error: (e as Error).message } }));
    }
  };
  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  if (editing)
    return (
      <ServerForm
        server={editing === "new" ? null : editing}
        taken={(data?.servers ?? []).map((s) => s.name)}
        onCancel={() => setEditing(null)}
        onSaved={async (name) => {
          setEditing(null);
          await load();
          test(name);
        }}
      />
    );

  const servers = data?.servers ?? [];
  return (
    <AppDialog
      title="MCP servers for the agent"
      subtitle={data?.shared ? "Everyone who can open this server shares this list." : "Only your chats with the agent use them."}
      width={640}
      onClose={props.onClose}
      footer={
        <>
          <Button label="Done" onClick={props.onClose} />
          <Button variant="primary" label="Add a server" icon={<IconPlus />} onClick={() => setEditing("new")} />
        </>
      }
    >
      <VStack gap={4}>
        <Text color="secondary">
          The agent connects to these when you send it a message and can call their tools as well as exuvia's own, such as
          your tracker, your docs or your company's tools behind a Portkey gateway. Their tools act outside exuvia, as you.
        </Text>
        {err && <Banner role="alert" status="error" title={err} />}
        {data === null && !err ? (
          <Center padding={4}>
            <Spinner label="Loading…" />
          </Center>
        ) : servers.length === 0 ? (
          <Text type="supporting" color="secondary">
            No servers yet. Add one with its URL and the header that signs you in, such as{" "}
            <Code>{PORTKEY_HEADER}</Code> for Portkey or <Code>Authorization</Code> for a bearer token.
          </Text>
        ) : (
          <List hasDividers edge="flush">
            {servers.map((s) => (
              <ListItem
                key={s.name}
                align="first-line"
                label={
                  <HStack gap={2} vAlign="center">
                    <Text weight="semibold">{s.name}</Text>
                    <Text type="supporting" color="secondary" maxLines={1}>
                      {s.url}
                    </Text>
                  </HStack>
                }
                description={<ServerLine server={s} test={tests[s.name]} />}
                endContent={
                  <HStack gap={1} vAlign="center">
                    <Switch
                      size="sm"
                      label={`Use ${s.name}`}
                      isLabelHidden
                      value={s.enabled}
                      onChange={(on) => act(() => agentMcpApi.put(s.name, { enabled: on }))}
                    />
                    <Button size="sm" variant="ghost" label="Test" isDisabled={tests[s.name] === "running"} onClick={() => test(s.name)} />
                    <Button size="sm" variant="ghost" label="Edit" onClick={() => setEditing(s)} />
                    <IconButton size="sm" variant="ghost" label={`Remove ${s.name}`} tooltip={`Remove ${s.name}`} icon={<IconTrash />} onClick={() => setRemoving(s)} />
                  </HStack>
                }
              />
            ))}
          </List>
        )}
      </VStack>
      <AlertDialog
        isOpen={!!removing}
        onOpenChange={(open) => {
          if (!open) setRemoving(null);
        }}
        title={`Remove ${removing?.name ?? "this server"}?`}
        description="The agent stops using its tools, and its headers are deleted from this server."
        actionLabel="Remove"
        actionVariant="destructive"
        onAction={async () => {
          const s = removing;
          setRemoving(null);
          if (s) await act(() => agentMcpApi.remove(s.name));
        }}
      />
    </AppDialog>
  );
}

/** Under a server's name: its headers, then what its last test found. */
function ServerLine(props: { server: AgentMcpServer; test?: AgentMcpTest | "running" }) {
  const { server, test } = props;
  const headers = server.headers.length
    ? server.headers.map((h) => (h.hint ? `${h.name} …${h.hint}` : h.name)).join(", ")
    : "no headers";
  return (
    <VStack gap={1}>
      <Text type="supporting" color="secondary">
        {server.enabled ? headers : `Off · ${headers}`}
      </Text>
      {test === "running" ? (
        <Text type="supporting" color="secondary">
          Connecting…
        </Text>
      ) : test?.ok ? (
        <Text type="supporting" xstyle={statusText.success}>
          Connected: {test.tools.length} tool{test.tools.length === 1 ? "" : "s"}
          {test.tools.length > 0 && ` (${test.tools.slice(0, 6).map((t) => t.name).join(", ")}${test.tools.length > 6 ? ", …" : ""})`}
        </Text>
      ) : test ? (
        <Text type="supporting" xstyle={statusText.error}>
          {test.error}
        </Text>
      ) : null}
    </VStack>
  );
}

let rowKey = 0;
const row = (r: Omit<HeaderRow, "key">): HeaderRow => ({ ...r, key: ++rowKey });

/** Add a server, or change one: its URL and its headers. */
function ServerForm(props: { server: AgentMcpServer | null; taken: string[]; onCancel: () => void; onSaved: (name: string) => void }) {
  const { server } = props;
  const [name, setName] = useState(server?.name ?? "");
  const [url, setUrl] = useState(server?.url ?? "");
  const [rows, setRows] = useState<HeaderRow[]>(() =>
    server ? server.headers.map((h) => row({ name: h.name, value: "", hint: h.hint })) : [row({ name: "", value: "" })],
  );
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const nameProblem =
    server || !name ? null : !NAME.test(name) ? "lowercase letters, digits and _, starting with a letter" : props.taken.includes(name) ? "you already have one by that name" : null;

  const portkey = () => {
    if (!url) setUrl(PORTKEY_URL);
    if (!rows.some((r) => !r.removed && r.name.toLowerCase() === PORTKEY_HEADER)) {
      const blank = rows.findIndex((r) => !r.hint && !r.name && !r.value);
      const next = row({ name: PORTKEY_HEADER, value: "" });
      setRows(blank >= 0 ? rows.map((r, i) => (i === blank ? next : r)) : [...rows, next]);
    }
  };
  const edit = (key: number, patch: Partial<HeaderRow>) => setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));

  const save = async () => {
    if (busy || nameProblem || !name) return;
    const headers: Record<string, string | null> = {};
    for (const r of rows) {
      const n = r.name.trim();
      if (r.hint !== undefined) {
        if (r.removed) headers[n] = null;
        else if (r.value) headers[n] = r.value;
      } else if (n || r.value) {
        if (!n) return setErr("Give each header a name.");
        if (!r.value) return setErr(`Give ${n} a value.`);
        headers[n] = r.value;
      }
    }
    setErr(null);
    setBusy(true);
    try {
      await agentMcpApi.put(name, { url: url.trim(), headers });
      props.onSaved(name);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AppDialog
      title={server ? `Edit ${server.name}` : "Add an MCP server"}
      subtitle="A remote MCP server (Streamable HTTP) and the headers that sign you in."
      width={640}
      onClose={props.onCancel}
      onSubmit={save}
      footer={(formId) => (
        <>
          <Button label="Cancel" onClick={props.onCancel} />
          <Button type="submit" form={formId} variant="primary" label={server ? "Save" : "Add"} isDisabled={busy || !name || !url || !!nameProblem} />
        </>
      )}
    >
      <VStack gap={4}>
        {err && <Banner role="alert" status="error" title={err} />}
        {!server && (
          <TextInput
            label="Name"
            description="Its tools reach the agent as name__tool."
            placeholder="linear"
            hasAutoFocus
            value={name}
            onChange={(v) => setName(v.trim().toLowerCase().slice(0, 24))}
            status={nameProblem ? { type: "error", message: nameProblem } : undefined}
          />
        )}
        <VStack gap={2}>
          <TextInput
            label="URL"
            type="url"
            placeholder="https://mcp.example.com/mcp"
            value={url}
            onChange={setUrl}
            description="Through Portkey's MCP gateway: https://mcp.portkey.ai/<server>/mcp, with your Portkey API key in x-portkey-api-key."
          />
          <HStack>
            <Button size="sm" variant="secondary" label="Use Portkey" onClick={portkey} />
          </HStack>
        </VStack>
        <VStack gap={2}>
          <Heading level={3}>Headers</Heading>
          <Text type="supporting" color="secondary">
            Sent with every request to this server, such as Authorization: Bearer … or x-portkey-api-key. Values are kept on this exuvia
            server and never shown again.
          </Text>
          {rows.map((r) =>
            r.hint !== undefined ? (
              <HStack key={r.key} gap={2} vAlign="center">
                <Text type="code" xstyle={styles.headerName}>
                  {r.name}
                </Text>
                <TextInput
                  label={`New value for ${r.name}`}
                  isLabelHidden
                  type="password"
                  autoComplete="off"
                  containerXstyle={styles.grow}
                  isDisabled={r.removed}
                  placeholder={r.removed ? "removed when you save" : r.hint ? `unchanged (…${r.hint})` : "unchanged"}
                  value={r.value}
                  onChange={(v) => edit(r.key, { value: v })}
                />
                <Button
                  size="sm"
                  variant="ghost"
                  label={r.removed ? "Keep" : "Remove"}
                  onClick={() => edit(r.key, { removed: !r.removed, value: "" })}
                />
              </HStack>
            ) : (
              <HStack key={r.key} gap={2} vAlign="center">
                <TextInput
                  label="Header name"
                  isLabelHidden
                  autoComplete="off"
                  containerXstyle={styles.headerName}
                  placeholder="Authorization"
                  value={r.name}
                  onChange={(v) => edit(r.key, { name: v.trim() })}
                />
                <TextInput
                  label={`Value of ${r.name || "the header"}`}
                  isLabelHidden
                  type="password"
                  autoComplete="off"
                  containerXstyle={styles.grow}
                  placeholder={r.name.toLowerCase() === "authorization" ? "Bearer …" : "value"}
                  value={r.value}
                  onChange={(v) => edit(r.key, { value: v })}
                />
                <IconButton
                  size="sm"
                  variant="ghost"
                  label="Remove this header"
                  tooltip="Remove this header"
                  icon={<IconTrash />}
                  onClick={() => setRows((rs) => rs.filter((x) => x.key !== r.key))}
                />
              </HStack>
            ),
          )}
          <HStack>
            <Button size="sm" variant="ghost" icon={<IconPlus />} label="Add a header" onClick={() => setRows((rs) => [...rs, row({ name: "", value: "" })])} />
          </HStack>
        </VStack>
      </VStack>
    </AppDialog>
  );
}

const styles = stylex.create({
  grow: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0 },
  headerName: { flexBasis: "200px", flexShrink: 0, minWidth: 0 },
});
