import { useCallback, useEffect, useState } from "react";
import { tokensApi, type AgentToken } from "./api";
import { Code } from "./Code";
import { Dialog } from "./Dialog";
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

  return (
    <Dialog title="Connect an agent to this hub" className="connect-dialog" onClose={props.onClose}>
        <p className="hub-muted">
          Over MCP, your coding agent works in every project here: it lists and creates projects, and reads, writes and
          runs their nodes, and you see each change in the workbench as it happens. One connection covers the whole
          hub.{" "}
          {setup.endpoint ? (
            <>
              The hub is the MCP server, at <span className="mono">{setup.endpoint}</span>: nothing to install.
            </>
          ) : (
            <>
              It needs <a href="https://docs.astral.sh/uv/" target="_blank" rel="noreferrer">uv</a> (or pipx) on its machine.
            </>
          )}
        </p>
        {who === undefined ? (
          <p className="hub-muted">Loading…</p>
        ) : personal ? (
          <NewToken made={made} onMade={setMade} />
        ) : (
          setup.saveToken && (
            <div className="connect-step">
              <div className="connect-label">1. Save the hub's API token in a file only you can read</div>
              <Code text={setup.saveToken} />
              <div className="hub-muted connect-note">Run it, paste the token, press Enter, then Ctrl-D. Agents read the token from this file; it never goes into their config.</div>
            </div>
          )
        )}
        {showCommands && (
          <div className="connect-step">
            <div className="connect-label">{personal || setup.saveToken ? "2. " : ""}{personal ? "Copy it into your agent" : "Add the MCP server"}</div>
            <div className="seg connect-tabs" role="tablist">
              {CLIENTS.map((c) => (
                <button key={c.id} type="button" role="tab" aria-selected={client === c.id} className={client === c.id ? "on" : ""} onClick={() => setClient(c.id)}>
                  {c.label}
                </button>
              ))}
            </div>
            {client === "codex" && setup.codexToken && (
              <>
                <div className="hub-muted connect-note">
                  Codex reads the token from the <span className="mono">EXUVIA_API_TOKEN</span> environment variable. On macOS
                  or Linux, add this to your shell profile (<span className="mono">~/.zshrc</span>,{" "}
                  <span className="mono">~/.bashrc</span>) and open a new terminal:
                </div>
                <Code text={setup.codexToken.posix} />
                <div className="hub-muted connect-note">On Windows, run this, then open a new terminal:</div>
                <Code text={setup.codexToken.windows} />
                <div className="hub-muted connect-note">Then add the server:</div>
              </>
            )}
            <Code text={setup[client]} wrap={client === "prompt"} />
            <div className="hub-muted connect-note">
              {current.hint}
              {personal && " Your token is in it: keep it to yourself, and don't commit it."}
            </div>
          </div>
        )}
        {personal && <TokenList fresh={made?.id} />}
        <div className="actions">
          <button type="button" className="btn" onClick={props.onClose}>Done</button>
        </div>
    </Dialog>
  );
}

/** Step 1 for someone signed in: make an agent token, named for where it goes. */
function NewToken(props: { made: (AgentToken & { token: string }) | null; onMade: (t: AgentToken & { token: string }) => void }) {
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
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
      <div className="connect-step">
        <div className="connect-label">1. Your agent token</div>
        <div className="hub-muted connect-note">
          <span className="mono">{props.made.name}</span> (…{props.made.hint}) is ready, and already in the commands below. It
          signs the agent in as you, and this is the only time it is shown: if you lose it, make another.
        </div>
      </div>
    );
  return (
    <form className="connect-step" onSubmit={create}>
      <label className="connect-label" htmlFor="token-name">1. Make an agent token</label>
      <div className="hub-muted connect-note">
        A token of your own: the agent signs in as you, and what it changes and runs is recorded as yours. Revoke it here
        whenever you like.
      </div>
      <div className="token-new">
        <input id="token-name" autoFocus maxLength={80} placeholder="What it's for, e.g. Claude Code on my laptop" value={name} onChange={(e) => setName(e.target.value)} />
        <button type="submit" className="btn primary" disabled={busy}>Create token</button>
      </div>
      {err && <div className="form-err" role="alert">{err}</div>}
    </form>
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
    <div className="connect-step">
      <div className="connect-label">Your tokens</div>
      {err && <div className="form-err" role="alert">{err}</div>}
      {tokens && (
        <ul className="token-list">
          {tokens.map((t) => (
            <li key={t.id}>
              <span className="token-name">{t.name}</span>
              <span className="hub-muted mono">…{t.hint}</span>
              <span className="hub-muted">{t.last_used ? `used ${ago(t.last_used)}` : "never used"}</span>
              <button type="button" className="btn ghost sm danger" onClick={() => revoke(t)} title={`Revoke ${t.name}: agents using it are turned away`}>
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
