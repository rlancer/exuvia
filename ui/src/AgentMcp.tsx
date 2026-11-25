import { useCallback, useEffect, useRef, useState } from "react";
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
import {
  agentMcpApi,
  mcpPresetsApi,
  type AgentMcpList,
  type AgentMcpPreset,
  type AgentMcpServer,
  type AgentMcpSignedIn,
  type AgentMcpTest,
} from "./api";
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
  const [tests, setTests] = useState<Record<string, Status>>({});
  const [removing, setRemoving] = useState<AgentMcpServer | null>(null);
  // A server on offer whose headers you are filling in to connect it.
  const [connecting, setConnecting] = useState<AgentMcpPreset | null>(null);
  const watching = useRef<number | null>(null);

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

  // The OAuth callback page tells this window how the sign-in went (exuvia/server.py).
  useEffect(() => {
    const onMessage = (ev: MessageEvent) => {
      const m = ev.data as AgentMcpSignedIn | undefined;
      if (ev.origin !== window.location.origin || m?.type !== "exuvia-mcp-oauth") return;
      const result: AgentMcpTest = m.ok ? { ok: true, tools: m.tools } : { ok: false, error: m.error, signin: m.signin };
      setTests((t) => ({ ...t, [m.name]: result }));
      load();
    };
    window.addEventListener("message", onMessage);
    return () => {
      window.removeEventListener("message", onMessage);
      if (watching.current !== null) window.clearInterval(watching.current);
    };
  }, [load]);

  const signIn = async (name: string) => {
    // Opened in the click, so the browser lets it open, and sent to the sign-in page once the server answers.
    const popup = window.open("", "exuvia-mcp-oauth", "popup,width=520,height=720");
    setTests((t) => ({ ...t, [name]: "signing-in" }));
    const show = (r: AgentMcpTest) => setTests((t) => ({ ...t, [name]: r }));
    try {
      const r = await agentMcpApi.signIn(name, window.location.origin, window.location.href);
      if (!("url" in r)) {
        popup?.close();
        show(r);
        await load();
        return;
      }
      // No popup (the browser blocked it): sign in in this tab, which the callback sends back here.
      if (!popup) return window.location.assign(r.url);
      popup.location.href = r.url;
      // Closed without finishing: back to how it was.
      if (watching.current !== null) window.clearInterval(watching.current);
      watching.current = window.setInterval(() => {
        if (!popup.closed) return;
        if (watching.current !== null) window.clearInterval(watching.current);
        watching.current = null;
        setTests((t) => {
          if (t[name] !== "signing-in") return t;
          const { [name]: _, ...rest } = t;
          return rest;
        });
      }, 1000);
    } catch (e) {
      popup?.close();
      show({ ok: false, error: (e as Error).message });
    }
  };
  const signOut = (name: string) =>
    act(async () => {
      await agentMcpApi.signOut(name);
      setTests((t) => {
        const { [name]: _, ...rest } = t;
        return rest;
      });
    });

  /** Connect a server on offer: straight away when it asks for no headers, else through its form. */
  const connect = async (p: AgentMcpPreset) => {
    if (p.headers.length) return setConnecting(p);
    setErr(null);
    try {
      await agentMcpApi.connect(p.name, {});
      await load();
      test(p.name);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  if (connecting)
    return (
      <ConnectForm
        preset={connecting}
        onCancel={() => setConnecting(null)}
        onSaved={async (name) => {
          setConnecting(null);
          await load();
          test(name);
        }}
      />
    );

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
  const yours = new Set(servers.map((s) => s.name));
  const onOffer = (data?.presets ?? []).filter((p) => !yours.has(p.name));
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
        ) : servers.length === 0 && onOffer.length > 0 ? (
          <Text type="supporting" color="secondary">
            None of yours yet. Connect one this server offers below, or add your own.
          </Text>
        ) : servers.length === 0 ? (
          <Text type="supporting" color="secondary">
            No servers yet. Add one with its URL and the header that signs you in, such as{" "}
            <Code>{PORTKEY_HEADER}</Code> for Portkey or <Code>Authorization</Code> for a bearer token. A server that signs you in with
            OAuth needs no header: add it, then Sign in.
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
                    {wantsSignIn(tests[s.name]) ? (
                      <Button
                        size="sm"
                        variant="secondary"
                        label={s.signed_in ? "Sign in again" : "Sign in"}
                        isDisabled={tests[s.name] === "signing-in"}
                        onClick={() => signIn(s.name)}
                      />
                    ) : (
                      s.signed_in && <Button size="sm" variant="ghost" label="Sign out" onClick={() => signOut(s.name)} />
                    )}
                    <Button size="sm" variant="ghost" label="Test" isDisabled={busy(tests[s.name])} onClick={() => test(s.name)} />
                    <Button size="sm" variant="ghost" label="Edit" onClick={() => setEditing(s)} />
                    <IconButton size="sm" variant="ghost" label={`Remove ${s.name}`} tooltip={`Remove ${s.name}`} icon={<IconTrash />} onClick={() => setRemoving(s)} />
                  </HStack>
                }
              />
            ))}
          </List>
        )}
        {onOffer.length > 0 && (
          <VStack gap={2}>
            <Heading level={3}>On offer here</Heading>
            <Text type="supporting" color="secondary">
              Set up by an admin of this server for anyone to use. The agent uses one only once you connect it.
            </Text>
            <List hasDividers edge="flush">
              {onOffer.map((p) => (
                <ListItem
                  key={p.name}
                  align="first-line"
                  label={
                    <HStack gap={2} vAlign="center">
                      <Text weight="semibold">{p.name}</Text>
                      <Text type="supporting" color="secondary" maxLines={1}>
                        {p.url}
                      </Text>
                    </HStack>
                  }
                  description={<PresetLine preset={p} />}
                  endContent={<Button size="sm" variant="secondary" label="Connect" onClick={() => connect(p)} />}
                />
              ))}
            </List>
          </VStack>
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

/** A server's last connection: running, waiting for an OAuth sign-in in another window, or its outcome. */
type Status = AgentMcpTest | "running" | "signing-in";

const busy = (s?: Status) => s === "running" || s === "signing-in";
/** Whether to offer Sign in: the server asked for an OAuth sign-in, or one is under way. */
const wantsSignIn = (s?: Status) => s === "signing-in" || (typeof s === "object" && !s.ok && !!s.signin);

/** Under a server's name: how it signs you in, then what its last test found. */
function ServerLine(props: { server: AgentMcpServer; test?: Status }) {
  const { server, test } = props;
  const how = [
    ...server.headers.map((h) => (h.hint ? `${h.name} …${h.hint}` : h.name)),
    ...(server.signed_in ? ["signed in with OAuth"] : []),
  ];
  const headers = how.length ? how.join(", ") : "no headers";
  return (
    <VStack gap={1}>
      <Text type="supporting" color="secondary">
        {server.enabled ? headers : `Off · ${headers}`}
      </Text>
      {test === "running" ? (
        <Text type="supporting" color="secondary">
          Connecting…
        </Text>
      ) : test === "signing-in" ? (
        <Text type="supporting" color="secondary">
          Waiting for you to sign in in the other window…
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

/** Under a server on offer: what it is, and what connecting it asks you for. */
function PresetLine(props: { preset: AgentMcpPreset }) {
  const { preset } = props;
  const asks = preset.headers.length ? `Asks for ${preset.headers.join(", ")}` : "Asks for nothing, or an OAuth sign-in after you connect";
  return (
    <VStack gap={1}>
      {preset.description && <Text type="supporting">{preset.description}</Text>}
      <Text type="supporting" color="secondary">
        {asks}
      </Text>
    </VStack>
  );
}

/** Connect a server on offer: a value for each header it asks for. */
function ConnectForm(props: { preset: AgentMcpPreset; onCancel: () => void; onSaved: (name: string) => void }) {
  const { preset } = props;
  const [values, setValues] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const complete = preset.headers.every((h) => values[h]);

  const save = async () => {
    if (busy || !complete) return;
    setErr(null);
    setBusy(true);
    try {
      await agentMcpApi.connect(preset.name, values);
      props.onSaved(preset.name);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AppDialog
      title={`Connect ${preset.name}`}
      subtitle={preset.url}
      width={560}
      onClose={props.onCancel}
      onSubmit={save}
      footer={(formId) => (
        <>
          <Button label="Cancel" onClick={props.onCancel} />
          <Button type="submit" form={formId} variant="primary" label="Connect" isDisabled={busy || !complete} />
        </>
      )}
    >
      <VStack gap={4}>
        {err && <Banner role="alert" status="error" title={err} />}
        {preset.description && <Text>{preset.description}</Text>}
        <Text color="secondary">
          Your values go with every request the agent makes to it for you. They are kept on this exuvia server and never shown again.
        </Text>
        {preset.headers.map((h, i) => (
          <TextInput
            key={h}
            label={h}
            type="password"
            autoComplete="off"
            hasAutoFocus={i === 0}
            placeholder={h.toLowerCase() === "authorization" ? "Bearer …" : "value"}
            value={values[h] ?? ""}
            onChange={(v) => setValues((vs) => ({ ...vs, [h]: v }))}
          />
        ))}
      </VStack>
    </AppDialog>
  );
}

/**
 * The MCP servers this server offers everyone, for an admin to change. Nobody's agent uses one until
 * they press Connect in their own MCP servers menu, which copies it onto their list.
 */
export function McpPresetsDialog(props: { onClose: () => void }) {
  const [presets, setPresets] = useState<AgentMcpPreset[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState<AgentMcpPreset | "new" | null>(null);
  const [removing, setRemoving] = useState<AgentMcpPreset | null>(null);

  const load = useCallback(
    () =>
      mcpPresetsApi
        .list()
        .then((r) => setPresets(r.presets))
        .catch((e: Error) => setErr(e.message)),
    [],
  );
  useEffect(() => {
    load();
  }, [load]);

  if (editing)
    return (
      <PresetForm
        preset={editing === "new" ? null : editing}
        taken={(presets ?? []).map((p) => p.name)}
        onCancel={() => setEditing(null)}
        onSaved={async () => {
          setEditing(null);
          await load();
        }}
      />
    );

  return (
    <AppDialog
      title="MCP servers on offer"
      subtitle="Everyone can connect these to their agent; nobody has them until they do."
      width={640}
      onClose={props.onClose}
      footer={
        <>
          <Button label="Done" onClick={props.onClose} />
          <Button variant="primary" label="Offer a server" icon={<IconPlus />} onClick={() => setEditing("new")} />
        </>
      }
    >
      <VStack gap={4}>
        <Text color="secondary">
          Each person finds these in the agent's MCP servers menu and presses Connect, typing their own key if the server asks for
          one, or signing in with OAuth. A change here, or taking one off offer, leaves the copies people already connected as
          they are.
        </Text>
        {err && <Banner role="alert" status="error" title={err} />}
        {presets === null && !err ? (
          <Center padding={4}>
            <Spinner label="Loading…" />
          </Center>
        ) : presets?.length === 0 ? (
          <Text type="supporting" color="secondary">
            Nothing on offer yet. Offer a server with its URL, and the names of the headers each person fills in with their own
            value, such as <Code>Authorization</Code> or <Code>{PORTKEY_HEADER}</Code>. A server that signs people in with OAuth asks
            for none.
          </Text>
        ) : (
          <List hasDividers edge="flush">
            {(presets ?? []).map((p) => (
              <ListItem
                key={p.name}
                align="first-line"
                label={
                  <HStack gap={2} vAlign="center">
                    <Text weight="semibold">{p.name}</Text>
                    <Text type="supporting" color="secondary" maxLines={1}>
                      {p.url}
                    </Text>
                  </HStack>
                }
                description={<PresetLine preset={p} />}
                endContent={
                  <HStack gap={1} vAlign="center">
                    <Button size="sm" variant="ghost" label="Edit" onClick={() => setEditing(p)} />
                    <IconButton
                      size="sm"
                      variant="ghost"
                      label={`Stop offering ${p.name}`}
                      tooltip={`Stop offering ${p.name}`}
                      icon={<IconTrash />}
                      onClick={() => setRemoving(p)}
                    />
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
        title={`Stop offering ${removing?.name ?? "this server"}?`}
        description="Nobody else can connect it. Whoever already did keeps it on their list."
        actionLabel="Stop offering"
        actionVariant="destructive"
        onAction={async () => {
          const p = removing;
          setRemoving(null);
          if (!p) return;
          setErr(null);
          try {
            await mcpPresetsApi.remove(p.name);
            await load();
          } catch (e) {
            setErr((e as Error).message);
          }
        }}
      />
    </AppDialog>
  );
}

/** Offer a server, or change one on offer: its URL, a line on what it is, and the headers each person fills in. */
function PresetForm(props: { preset: AgentMcpPreset | null; taken: string[]; onCancel: () => void; onSaved: () => void }) {
  const { preset } = props;
  const [name, setName] = useState(preset?.name ?? "");
  const [url, setUrl] = useState(preset?.url ?? "");
  const [description, setDescription] = useState(preset?.description ?? "");
  const [headers, setHeaders] = useState((preset?.headers ?? []).join(", "));
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const nameProblem =
    preset || !name ? null : !NAME.test(name) ? "lowercase letters, digits and _, starting with a letter" : props.taken.includes(name) ? "one is on offer by that name" : null;

  const save = async () => {
    if (busy || nameProblem || !name || !url.trim()) return;
    setErr(null);
    setBusy(true);
    try {
      const names = headers
        .split(",")
        .map((h) => h.trim())
        .filter(Boolean);
      await mcpPresetsApi.put(name, { url: url.trim(), description: description.trim(), headers: names });
      props.onSaved();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AppDialog
      title={preset ? `Edit ${preset.name}` : "Offer an MCP server"}
      subtitle="A remote MCP server (Streamable HTTP) everyone can connect their agent to."
      width={640}
      onClose={props.onCancel}
      onSubmit={save}
      footer={(formId) => (
        <>
          <Button label="Cancel" onClick={props.onCancel} />
          <Button
            type="submit"
            form={formId}
            variant="primary"
            label={preset ? "Save" : "Offer"}
            isDisabled={busy || !name || !url.trim() || !!nameProblem}
          />
        </>
      )}
    >
      <VStack gap={4}>
        {err && <Banner role="alert" status="error" title={err} />}
        {!preset && (
          <TextInput
            label="Name"
            description="The name it gets on each person's list; its tools reach the agent as name__tool."
            placeholder="linear"
            hasAutoFocus
            value={name}
            onChange={(v) => setName(v.trim().toLowerCase().slice(0, 24))}
            status={nameProblem ? { type: "error", message: nameProblem } : undefined}
          />
        )}
        <TextInput label="URL" type="url" placeholder="https://mcp.example.com/mcp" value={url} onChange={setUrl} />
        <TextInput
          label="What it is"
          description="One line people see next to it."
          placeholder="Our issue tracker"
          value={description}
          onChange={(v) => setDescription(v.slice(0, 300))}
        />
        <TextInput
          label="Headers each person fills in"
          description="Names only, separated by commas: each person types their own value when they connect. Leave it empty for a server that signs people in with OAuth, or needs nothing."
          placeholder={`Authorization, ${PORTKEY_HEADER}`}
          value={headers}
          onChange={setHeaders}
        />
      </VStack>
    </AppDialog>
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
            server and never shown again. A server that signs you in with OAuth needs none: save it, and Sign in appears next to it.
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
