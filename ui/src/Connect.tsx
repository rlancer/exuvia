import { useCallback, useEffect, useId, useState, type ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
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
import { tokensApi, type AgentToken } from "./api";
import { Code } from "./Code";
import { AppDialog } from "./ui/AppDialog";
import { mcpSetup } from "./mcp";
import { useMe } from "./useMe";
import { ago } from "./util";

type Client = "prompt" | "claude" | "codex" | "json";
const CLIENTS: { id: Client; label: string; hint: string }[] = [
  { id: "prompt", label: "Ask your agent", hint: "Paste this into Claude Code, Cursor, Codex, Grok or any coding agent with a terminal: it adds the server itself." },
  { id: "claude", label: "Claude Code", hint: "Run this in a terminal. The tools are there from your next session." },
  { id: "codex", label: "Codex", hint: "Run this in a terminal. The tools are there from your next session." },
  { id: "json", label: "JSON config", hint: "Add this under mcpServers in your client's MCP config (Cursor, a project's .mcp.json, ...)." },
];

/** A hint under a step's title or beside a block to copy. */
function Note({ children }: { children: ReactNode }) {
  return (
    <Text type="supporting" color="secondary">
      {children}
    </Text>
  );
}

/** One step of the setup: its title, then what to read, type or copy. */
function Step({ title, children }: { title: string; children: ReactNode }) {
  return (
    <VStack gap={2}>
      <Heading level={3}>{title}</Heading>
      {children}
    </VStack>
  );
}

/**
 * The MCP setup for the hub. Someone signed in through the sign-in proxy
 * makes an agent token of their own, and every command has it in place, so
 * they copy one and paste it with nothing else to do. Otherwise the hub's
 * shared API token goes in a file first, then one client's command.
 */
export function ConnectDialog(props: { auth?: boolean; onClose: () => void }) {
  const [client, setClient] = useState<Client>("prompt");
  const who = useMe();
  const [made, setMade] = useState<(AgentToken & { token: string }) | null>(null);
  // A request signed in with an agent token can't make another one.
  const personal = !!(who?.tokens && who.user && who.user.method !== "token");
  const setup = mcpSetup({ origin: location.origin, host: location.host, auth: !!props.auth, token: made?.token });
  const current = CLIENTS.find((c) => c.id === client)!;
  const showCommands = who !== undefined && (!personal || made);
  const panel = useId();

  return (
    <AppDialog title="Connect an agent to this hub" width={620} onClose={props.onClose} footer={<Button label="Done" onClick={props.onClose} />}>
      <VStack gap={5}>
        <Text color="secondary">
          Over MCP, your coding agent works in every project here: it lists and creates projects, and reads, writes and
          runs their nodes, and you see each change in the workbench as it happens. One connection covers the whole
          hub.{" "}
          {setup.endpoint ? (
            <>
              The hub is the MCP server, at <InlineCode>{setup.endpoint}</InlineCode>: nothing to install.
            </>
          ) : (
            <>
              It needs{" "}
              <Link href="https://docs.astral.sh/uv/" isExternalLink>
                uv
              </Link>{" "}
              (or pipx) on its machine.
            </>
          )}
        </Text>
        {who === undefined ? (
          <Center padding={4}>
            <Spinner label="Loading…" />
          </Center>
        ) : personal ? (
          <NewToken made={made} onMade={setMade} />
        ) : (
          setup.saveToken && (
            <Step title="1. Save the hub's API token in a file only you can read">
              <Code text={setup.saveToken} />
              <Note>Run it, paste the token, press Enter, then Ctrl-D. Agents read the token from this file; it never goes into their config.</Note>
            </Step>
          )
        )}
        {showCommands && (
          <Step title={`${personal || setup.saveToken ? "2. " : ""}${personal ? "Copy it into your agent" : "Add the MCP server"}`}>
            <TabList role="tablist" size="sm" hasDivider value={client} onChange={(c) => setClient(c as Client)}>
              {CLIENTS.map((c) => (
                <Tab key={c.id} value={c.id} label={c.label} panelId={panel} />
              ))}
            </TabList>
            <VStack gap={2} id={panel} role="tabpanel" aria-label={current.label}>
              {client === "codex" && setup.codexToken && (
                <>
                  <Note>
                    Codex reads the token from the <InlineCode>EXUVIA_API_TOKEN</InlineCode> environment variable. On macOS or Linux, add this to
                    your shell profile (<InlineCode>~/.zshrc</InlineCode>, <InlineCode>~/.bashrc</InlineCode>) and open a new terminal:
                  </Note>
                  <Code text={setup.codexToken.posix} />
                  <Note>On Windows, run this, then open a new terminal:</Note>
                  <Code text={setup.codexToken.windows} />
                  <Note>Then add the server:</Note>
                </>
              )}
              <Code text={setup[client]} wrap={client === "prompt"} />
              <Note>
                {current.hint}
                {personal && " Your token is in it: keep it to yourself, and don't commit it."}
              </Note>
            </VStack>
          </Step>
        )}
        {personal && <TokenList fresh={made?.id} />}
      </VStack>
    </AppDialog>
  );
}

/** Step 1 for someone signed in: make an agent token, named for where it goes. */
function NewToken(props: { made: (AgentToken & { token: string }) | null; onMade: (t: AgentToken & { token: string }) => void }) {
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setErr(null);
    setBusy(true);
    try {
      props.onMade(await tokensApi.create(name.trim()));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (props.made)
    return (
      <Step title="1. Your agent token">
        <Note>
          <InlineCode>{props.made.name}</InlineCode> (…{props.made.hint}) is ready, and already in the commands below. It signs the agent in as
          you, and this is the only time it is shown: if you lose it, make another.
        </Note>
      </Step>
    );
  return (
    <Step title="1. Make an agent token">
      <Note>
        A token of your own: the agent signs in as you, and what it changes and runs is recorded as yours. Revoke it here whenever you
        like.
      </Note>
      <form onSubmit={create}>
        <HStack gap={2} vAlign="start">
          <TextInput
            label="What the token is for"
            isLabelHidden
            hasAutoFocus
            containerXstyle={styles.grow}
            placeholder="What it's for, e.g. Claude Code on my laptop"
            value={name}
            onChange={(v) => setName(v.slice(0, 80))}
            status={err ? { type: "error", message: err } : undefined}
            statusVariant="detached"
          />
          <Button type="submit" variant="primary" label="Create token" isDisabled={busy} />
        </HStack>
      </form>
    </Step>
  );
}

/** The signed-in person's agent tokens, each with a Revoke button. */
function TokenList(props: { fresh?: string }) {
  const [tokens, setTokens] = useState<AgentToken[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => tokensApi.list().then(setTokens).catch((e: Error) => setErr(e.message)), []);
  useEffect(() => {
    load();
  }, [load, props.fresh]);
  const revoke = async (t: AgentToken) => {
    setErr(null);
    try {
      await tokensApi.revoke(t.id);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  if (!tokens?.length && !err) return null;
  return (
    <Step title="Your tokens">
      {err && <Banner role="alert" status="error" title={err} />}
      {tokens && (
        <List hasDividers edge="flush">
          {tokens.map((t) => (
            <ListItem
              key={t.id}
              label={
                <HStack gap={2} vAlign="center">
                  <Text maxLines={1}>{t.name}</Text>
                  <Text type="code" color="secondary">
                    …{t.hint}
                  </Text>
                </HStack>
              }
              description={t.last_used ? `used ${ago(t.last_used)}` : "never used"}
              endContent={
                <Button
                  variant="ghost"
                  size="sm"
                  label="Revoke"
                  tooltip={`Revoke ${t.name}: agents using it are turned away`}
                  onClick={() => revoke(t)}
                />
              }
            />
          ))}
        </List>
      )}
    </Step>
  );
}

const styles = stylex.create({
  grow: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0 },
});
