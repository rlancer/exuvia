import { useCallback, useEffect, useState } from "react";
import { ApiError, hubApi, PROJECT_ID, type HubProject } from "./api";
import { ConnectDialog } from "./Connect";
import { SignedInAs } from "./Me";
import { HelpMenu } from "./Help";
import { useMe } from "./useMe";
import { useTheme } from "./theme";
import { Brand, Mark } from "./Brand";
import { Dialog } from "./Dialog";
import { IconMoon, IconPlus, IconSun } from "./icons";
import { Toasts } from "./toast";

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
  // Someone signed in reaches Connect agent from their account menu.
  const who = useMe();
  const signedIn = !!who?.user;

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
            {who !== undefined && !signedIn && (
              <button className="btn" onClick={() => setConnecting(true)} title="Connect a coding agent to every project on this hub over MCP">
                Connect agent
              </button>
            )}
            <button className="btn primary" onClick={() => setNewOpen(true)}><IconPlus size={12} /> Project</button>
          </>
        )}
        <HelpMenu />
        <button className="btn ghost icon-btn" onClick={toggleTheme} title={dark ? "Light theme" : "Dark theme"} aria-label={dark ? "Light theme" : "Dark theme"}>
          {dark ? <IconSun /> : <IconMoon />}
        </button>
        <SignedInAs hub />
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
