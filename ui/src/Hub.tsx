import { useCallback, useEffect, useState } from "react";
import { ApiError, hubApi, me, PROJECT_ID, tokensApi, type AgentToken, type HubProject, type Me } from "./api";
import { mcpSetup } from "./mcp";
import { SignedInAs } from "./Me";
import { useTheme } from "./theme";
import { Brand, Mark } from "./Brand";
import { Dialog } from "./Dialog";
import { IconMoon, IconPlus, IconSun } from "./icons";
import { Toasts } from "./toast";
import { ago } from "./util";

const workbench = (id: string) => `/p/${encodeURIComponent(id)}/`;

/** A project id from what is typed in the id field: spaces and other characters become `-`. */
export const toProjectId = (s: string) =>
  s
    .replace(/[^A-Za-z0-9_-]+/g, "-")
    .replace(/^[-_]+/, "")
    .slice(0, 64);

/** The id a project name suggests: "Market research" → "market-research". */
export const idFromName = (name: string) => toProjectId(name.trim().toLowerCase()).replace(/[-_]+$/, "");

type Listing = { projects: HubProject[]; templates: string[]; auth: boolean };

/**
 * The hub's home page, at `/` of `exuvia hub`: every project, with a button
 * to create one and a way to delete one; each opens its workbench at
 * `/p/<id>/`. Without the API token it asks for it.
 */
export default function Hub() {
  const [listing, setListing] = useState<Listing | null>(null);
  const [needToken, setNeedToken] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [deleting, setDeleting] = useState<HubProject | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [dark, toggleTheme] = useTheme();

  const load = useCallback(
    () =>
      hubApi
        .projects()
        .then((l) => {
          setListing(l);
          setNeedToken(false);
          setErr(null);
        })
        .catch((e: Error) => {
          if (e instanceof ApiError && e.status === 401) setNeedToken(true);
          else setErr(e.message);
        }),
    [],
  );

  useEffect(() => {
    load();
    // Agents add projects and nodes over the API: show them when the tab comes back.
    const onFocus = () => document.visibilityState === "visible" && load();
    document.addEventListener("visibilitychange", onFocus);
    return () => document.removeEventListener("visibilitychange", onFocus);
  }, [load]);

  const projects = listing?.projects ?? [];
  useEffect(() => {
    document.title = "Projects · exuvia";
  }, []);

  return (
    <div className="hub">
      <header className="topbar">
        <Brand>projects</Brand>
        <span className="spacer" />
        {listing && (
          <>
            <button className="btn" onClick={() => setConnecting(true)} title="Connect a coding agent to every project on this hub over MCP">
              Connect agent
            </button>
            <button className="btn primary" onClick={() => setNewOpen(true)}><IconPlus size={12} /> Project</button>
          </>
        )}
        <SignedInAs />
        <button className="btn ghost icon-btn" onClick={toggleTheme} title={dark ? "Light theme" : "Dark theme"} aria-label={dark ? "Light theme" : "Dark theme"}>
          {dark ? <IconSun /> : <IconMoon />}
        </button>
      </header>

      <main className="hub-main">
        {needToken ? (
          <TokenForm />
        ) : err ? (
          <div className="hub-card">
            <p className="form-err" role="alert">Couldn't load the projects: {err}</p>
            <button className="btn" onClick={load}>Try again</button>
          </div>
        ) : !listing ? (
          <p className="hub-muted">Loading…</p>
        ) : projects.length === 0 ? (
          <div className="hub-card hub-empty">
            <Mark size={40} />
            <h2>No projects yet</h2>
            <p>A project is one graph of Python and SQL nodes, with its own runs, outputs and views.</p>
            <button className="btn primary" onClick={() => setNewOpen(true)}><IconPlus size={12} /> New project</button>
          </div>
        ) : (
          <>
            <h1 className="hub-title">
              Projects <span className="hub-muted">{projects.length}</span>
            </h1>
            <ul className="hub-list">
              {projects.map((p) => (
                <li key={p.id}>
                  <a className="hub-open" href={workbench(p.id)}>
                    <span className="hub-name">{p.name || p.id}</span>
                    <span className="hub-meta">
                      <span className="mono">{p.id}</span> · {p.nodes} node{p.nodes === 1 ? "" : "s"}
                    </span>
                  </a>
                  <button className="btn ghost sm danger" onClick={() => setDeleting(p)} title={`Delete ${p.id}`}>
                    Delete
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </main>

      {newOpen && listing && (
        <NewProjectDialog
          templates={listing.templates}
          taken={projects.map((p) => p.id)}
          onClose={() => setNewOpen(false)}
          onCreated={(id) => location.assign(workbench(id))}
        />
      )}
      {connecting && listing && <ConnectDialog auth={listing.auth} onClose={() => setConnecting(false)} />}
      {deleting && (
        <DeleteDialog
          project={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={() => {
            setDeleting(null);
            load();
          }}
        />
      )}
      <Toasts />
    </div>
  );
}

/** Asks for the hub's API token, checks it, then has the server keep it in a cookie. */
function TokenForm() {
  const [token, setToken] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await hubApi.projects(token.trim());
      // The server answers /?token= with an HttpOnly cookie and a redirect back to /.
      location.assign(`/?token=${encodeURIComponent(token.trim())}`);
    } catch (e) {
      setBusy(false);
      setErr(e instanceof ApiError && e.status === 401 ? "That isn't this hub's token." : (e as Error).message);
    }
  };

  return (
    <form className="hub-card" onSubmit={submit}>
      <Mark size={40} />
      <h2>Sign in to this hub</h2>
      <p>Enter the hub's API token. This browser keeps it in a cookie, so you only enter it once.</p>
      <p className="hub-muted connect-note">
        Hub on a private network? Setting <code>EXUVIA_BROWSER_AUTH=open</code> on it lets browsers in without the token.
      </p>
      <div className="field">
        <label htmlFor="hub-token">API token</label>
        <input id="hub-token" type="password" className="mono" autoFocus autoComplete="current-password" value={token} onChange={(e) => setToken(e.target.value)} />
      </div>
      {err && <div className="form-err" role="alert">{err}</div>}
      <div className="actions">
        <button type="submit" className="btn primary" disabled={!token.trim() || busy}>Continue</button>
      </div>
    </form>
  );
}

function NewProjectDialog(props: { templates: string[]; taken: string[]; onClose: () => void; onCreated: (id: string) => void }) {
  const [name, setName] = useState("");
  const [id, setId] = useState("");
  const [idEdited, setIdEdited] = useState(false);
  const [template, setTemplate] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const finalId = idEdited ? id : idFromName(name);
  const problem = !finalId
    ? null
    : !PROJECT_ID.test(finalId)
      ? "Ids are up to 64 letters, digits, - or _, starting with a letter or digit."
      : props.taken.includes(finalId)
        ? `There is already a project ${finalId}.`
        : null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await hubApi.create({ id: finalId, name: name.trim() || undefined, template: template || undefined });
      props.onCreated(finalId);
    } catch (e) {
      setBusy(false);
      setErr((e as Error).message);
    }
  };

  return (
    <Dialog title="New project" onClose={props.onClose} onSubmit={submit}>
        <div className="field">
          <label htmlFor="np-name">Name</label>
          <input id="np-name" autoFocus value={name} placeholder="Market research" onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="np-id">Id (in its URL, /p/&lt;id&gt;/)</label>
          <input
            id="np-id"
            className="mono"
            value={finalId}
            placeholder="market-research"
            onChange={(e) => {
              setIdEdited(true);
              setId(toProjectId(e.target.value));
            }}
          />
        </div>
        {props.templates.length > 0 && (
          <div className="field">
            <label htmlFor="np-template">Start from</label>
            <select id="np-template" value={template} onChange={(e) => setTemplate(e.target.value)}>
              <option value="">An empty graph</option>
              {props.templates.map((t) => (
                <option key={t} value={t}>Template: {t}</option>
              ))}
            </select>
          </div>
        )}
        {(problem || err) && <div className="form-err" role="alert">{problem ?? err}</div>}
        <div className="actions">
          <button type="button" className="btn ghost" onClick={props.onClose}>Cancel</button>
          <button type="submit" className="btn primary" disabled={!finalId || !!problem || busy}>Create and open</button>
        </div>
    </Dialog>
  );
}

function DeleteDialog(props: { project: HubProject; onClose: () => void; onDeleted: () => void }) {
  const [typed, setTyped] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { id } = props.project;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await hubApi.remove(id);
      props.onDeleted();
    } catch (e) {
      setBusy(false);
      setErr((e as Error).message);
    }
  };

  return (
    <Dialog title={`Delete ${props.project.name || id}?`} onClose={props.onClose} onSubmit={submit}>
        <p className="hub-muted">Its code, run history and outputs are deleted. This can't be undone.</p>
        <div className="field">
          <label htmlFor="del-id">
            Type <span className="mono">{id}</span> to confirm
          </label>
          <input id="del-id" className="mono" autoFocus spellCheck={false} autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} />
        </div>
        {err && <div className="form-err" role="alert">{err}</div>}
        <div className="actions">
          <button type="button" className="btn ghost" onClick={props.onClose}>Cancel</button>
          <button type="submit" className="btn destroy" disabled={typed !== id || busy}>Delete project</button>
        </div>
    </Dialog>
  );
}

type Client = "prompt" | "claude" | "codex" | "json";
const CLIENTS: { id: Client; label: string; hint: string }[] = [
  { id: "prompt", label: "Ask your agent", hint: "Paste this into Claude Code, Cursor, Codex, Grok or any coding agent with a terminal: it adds the server itself." },
  { id: "claude", label: "Claude Code", hint: "Run this in a terminal. The tools are there from your next session." },
  { id: "codex", label: "Codex", hint: "Run this in a terminal. The tools are there from your next session." },
  { id: "json", label: "JSON config", hint: "Add this under mcpServers in your client's MCP config (Cursor, Claude Desktop, Windsurf, ...)." },
];

/**
 * The MCP setup for the hub. Someone signed in through the sign-in proxy
 * makes an agent token of their own, and every command has it in place, so
 * they copy one and paste it with nothing else to do. Otherwise the hub's
 * shared API token goes in a file first, then one client's command.
 */
function ConnectDialog(props: { auth: boolean; onClose: () => void }) {
  const [client, setClient] = useState<Client>("prompt");
  const [who, setWho] = useState<Me | null | undefined>(undefined);
  const [made, setMade] = useState<(AgentToken & { token: string }) | null>(null);
  useEffect(() => {
    me()
      .then(setWho)
      .catch(() => setWho(null)); // an older server
  }, []);
  // A request signed in with an agent token can't make another one.
  const personal = !!(who?.tokens && who.user && who.user.method !== "token");
  const setup = mcpSetup({ origin: location.origin, host: location.host, auth: props.auth, token: made?.token });
  const current = CLIENTS.find((c) => c.id === client)!;
  const showCommands = who !== undefined && (!personal || made);

  return (
    <Dialog title="Connect an agent to this hub" className="connect-dialog" onClose={props.onClose}>
        <p className="hub-muted">
          Over MCP, your coding agent works in every project here: it lists and creates projects, and reads, writes and
          runs their nodes, and you see each change in the workbench as it happens. One connection covers the whole
          hub. It needs <a href="https://docs.astral.sh/uv/" target="_blank" rel="noreferrer">uv</a>{" "}
          (or pipx) on its machine.
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

/** A block of text to copy, with a button that copies it. */
function Code(props: { text: string; wrap?: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(props.text);
    } catch {
      // No clipboard API (plain http on another host): copy through a selection.
      const t = document.createElement("textarea");
      t.value = props.text;
      document.body.appendChild(t);
      t.select();
      document.execCommand("copy");
      t.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="code-copy">
      <pre className={props.wrap ? "wrap" : ""}>
        {props.wrap
          ? props.text
          : // Lines break only at spaces: a browser would also break after `-`, splitting `--url`.
            props.text.split(/( )/).map((part, i) => (part === " " ? " " : <span key={i} className="tok">{part}</span>))}
      </pre>
      <button type="button" className="btn sm" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
    </div>
  );
}
