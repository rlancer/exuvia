import { useCallback, useEffect, useRef, useState } from "react";
import { sharingApi, type GeneralAccess, type Member, type Person, type Role, type Sharing } from "./api";
import { Dialog } from "./Dialog";
import { ago } from "./util";

export const GENERAL_LABEL: Record<GeneralAccess, string> = {
  private: "Only people added here",
  view: "Everyone signed in can view",
  edit: "Everyone signed in can edit",
};

const ROLE_LABEL: Record<Role, string> = { viewer: "Viewer", editor: "Editor", owner: "Owner" };

const EXPIRY: { label: string; days: number | null }[] = [
  { label: "Never expires", days: null },
  { label: "Expires in 1 day", days: 1 },
  { label: "Expires in 7 days", days: 7 },
  { label: "Expires in 30 days", days: 30 },
];

const looksLikeEmail = (s: string) => /^[^\s@]+@[^\s@]+$/.test(s.trim());
const who = (p: Person | null | undefined) => (p ? (p.email && p.email !== p.name ? `${p.name} · ${p.email}` : p.name) : "nobody");

/**
 * Share a hub project, as its owner (or an admin) does: add people as viewers
 * or editors (by name, or by email for someone who hasn't signed in yet), set
 * what everyone else signed in gets, make read-only links, answer access
 * requests and hand the project to a new owner. Everyone else who can open
 * the project sees who has access and can take themselves off it.
 */
export function ShareDialog(props: { project: string; name?: string; self: string | null; onClose: () => void; onChanged?: () => void }) {
  const { project, onChanged } = props;
  const [s, setS] = useState<Sharing | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [made, setMade] = useState<{ url: string; name: string } | null>(null);
  const [handOver, setHandOver] = useState<Member | null>(null);

  const load = useCallback(() => sharingApi.get(project).then(setS).catch((e: Error) => setErr(e.message)), [project]);
  useEffect(() => {
    load();
  }, [load]);

  /** Run a change; the answer is the new sharing (or a reason it failed). */
  const act = async (fn: () => Promise<Sharing | unknown>) => {
    setErr(null);
    setBusy(true);
    try {
      const r = await fn();
      if (r && typeof r === "object" && "project" in r) setS(r as Sharing);
      else await load();
      onChanged?.();
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const manage = !!s?.can_manage;
  const leave = async (m: Member) => {
    setErr(null);
    try {
      const r = await sharingApi.unshare(project, m.principal);
      onChanged?.();
      if ("left" in r) location.assign("/");
      else setS(r);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <Dialog title={`Share ${props.name || project}`} className="share-dialog" onClose={props.onClose}>
      {err && <div className="err-banner" role="alert"><div>{err}</div></div>}
      {!s ? (
        <div className="placeholder">{err ? "" : "Loading…"}</div>
      ) : (
        <>
          {manage && s.people_enabled && (
            <AddPeople
              busy={busy}
              have={new Set([s.owner?.id, ...s.people.map((m) => m.user.id)].filter((x): x is string => !!x))}
              onShare={(w) => act(() => sharingApi.share(project, w))}
            />
          )}

          {manage && !!s.requests?.length && (
            <section className="share-section">
              <h4>Asking for access</h4>
              <ul className="share-list">
                {s.requests.map((r) => (
                  <RequestRow
                    key={r.user_id}
                    name={who(r.user)}
                    asked={r.role}
                    message={r.message}
                    at={r.created_at}
                    busy={busy}
                    onApprove={(role) => act(() => sharingApi.approve(project, r.user_id, role))}
                    onDismiss={() => act(() => sharingApi.dismiss(project, r.user_id))}
                  />
                ))}
              </ul>
            </section>
          )}

          {s.people_enabled && (
            <section className="share-section">
              <h4>People with access</h4>
              <ul className="share-list">
                <li>
                  <span className="share-who">
                    {s.owner ? who(s.owner) : <span className="hub-muted">No owner: hub admins manage it</span>}
                    {s.owner && s.owner.id === props.self ? " (you)" : ""}
                  </span>
                  <span className="share-role">Owner</span>
                </li>
                {s.people.map((m) => {
                  const mine = m.kind === "user" && m.user.id === props.self;
                  return (
                    <li key={m.principal}>
                      <span className="share-who">
                        {who(m.user)}
                        {mine ? " (you)" : ""}
                        {m.pending && <span className="badge" title="Nobody has signed in with this email yet: it takes effect when they do">invited</span>}
                      </span>
                      {manage ? (
                        <>
                          <select
                            aria-label={`Role of ${m.user.name}`}
                            value={m.role}
                            disabled={busy}
                            onChange={(e) =>
                              act(() =>
                                sharingApi.share(project, {
                                  ...(m.kind === "user" ? { user: m.user.id! } : { email: m.user.email! }),
                                  role: e.target.value as Role,
                                }),
                              )
                            }
                          >
                            <option value="viewer">Viewer</option>
                            <option value="editor">Editor</option>
                          </select>
                          {m.kind === "user" && (
                            <button className="btn sm ghost" disabled={busy} onClick={() => setHandOver(m)} title="Make them the owner; you stay on as an editor">
                              Make owner
                            </button>
                          )}
                          <button className="btn sm ghost danger" disabled={busy} onClick={() => act(() => sharingApi.unshare(project, m.principal))} aria-label={`Remove ${m.user.name}`}>
                            Remove
                          </button>
                        </>
                      ) : (
                        <>
                          <span className="share-role">{ROLE_LABEL[m.role]}</span>
                          {mine && <button className="btn sm ghost" onClick={() => leave(m)}>Leave</button>}
                        </>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          )}

          {s.people_enabled && (
            <section className="share-section">
              <h4>General access</h4>
              {manage ? (
                <select
                  aria-label="General access"
                  value={s.general}
                  disabled={busy}
                  onChange={(e) => act(() => sharingApi.patch(project, { general: e.target.value as GeneralAccess }))}
                >
                  {(Object.keys(GENERAL_LABEL) as GeneralAccess[]).map((g) => (
                    <option key={g} value={g}>{GENERAL_LABEL[g]}</option>
                  ))}
                </select>
              ) : (
                <p>{GENERAL_LABEL[s.general]}</p>
              )}
              {s.default && <p className="hint">The hub's default, since the project has no setting of its own yet.</p>}
            </section>
          )}

          {manage && (
            <Links
              links={s.links ?? []}
              busy={busy}
              made={made}
              onCreate={async (name, days) => {
                setErr(null);
                setBusy(true);
                try {
                  const l = await sharingApi.createLink(project, { name, expires_in_days: days });
                  setMade({ url: location.origin + l.url, name: l.name });
                  await load();
                  onChanged?.();
                } catch (e) {
                  setErr((e as Error).message);
                } finally {
                  setBusy(false);
                }
              }}
              onRevoke={(id) => act(() => sharingApi.revokeLink(project, id))}
            />
          )}

          {!manage && (
            <p className="hint">
              You're {s.you === "editor" ? "an editor" : "a viewer"} here. Only the owner{s.owner ? ` (${s.owner.name})` : ""} or a hub admin changes who has access.
            </p>
          )}
        </>
      )}
      <div className="actions">
        <button className="btn" onClick={props.onClose}>Done</button>
      </div>

      {handOver && (
        <Dialog title={`Make ${handOver.user.name} the owner?`} onClose={() => setHandOver(null)}>
          <p>They'll manage who has access and can delete the project. The current owner stays on as an editor.</p>
          <div className="actions">
            <button className="btn ghost" onClick={() => setHandOver(null)}>Cancel</button>
            <button
              className="btn primary"
              disabled={busy}
              onClick={async () => {
                const m = handOver;
                setHandOver(null);
                await act(() => sharingApi.patch(project, { owner: m.user.id! }));
              }}
            >
              Make owner
            </button>
          </div>
        </Dialog>
      )}
    </Dialog>
  );
}

/** A name or email box that suggests people from the user list, a role, and Share. */
function AddPeople(props: {
  busy: boolean;
  /** The owner and everyone it is shared with: not suggested again. */
  have: Set<string>;
  onShare: (w: { user?: string; email?: string; role: Role }) => Promise<boolean>;
}) {
  const [q, setQ] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [found, setFound] = useState<Person[]>([]);
  const [picked, setPicked] = useState<Person | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (timer.current) window.clearTimeout(timer.current);
    if (!q.trim() || picked) {
      setFound([]);
      return;
    }
    timer.current = window.setTimeout(() => {
      sharingApi.people(q).then((ps) => setFound(ps.filter((p) => !p.id || !props.have.has(p.id)))).catch(() => setFound([]));
    }, 150);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, picked]);

  const target = picked ? { user: picked.id! } : looksLikeEmail(q) ? { email: q.trim() } : null;
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!target) return;
    if (await props.onShare({ ...target, role })) {
      setQ("");
      setPicked(null);
    }
  };

  return (
    <form className="share-add" onSubmit={submit}>
      <div className="share-add-row">
        <div className="share-search">
          <input
            aria-label="Add people by name or email"
            placeholder="Add people by name or email"
            value={picked ? who(picked) : q}
            onChange={(e) => {
              setPicked(null);
              setQ(e.target.value);
            }}
            autoComplete="off"
          />
          {found.length > 0 && (
            <ul className="share-suggest" role="listbox">
              {found.map((p) => (
                <li key={p.id} role="option" aria-selected={false}>
                  <button type="button" onClick={() => setPicked(p)}>
                    <span>{p.name}</span>
                    {p.email && <span className="hub-muted">{p.email}</span>}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <select aria-label="Role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="viewer">Viewer</option>
          <option value="editor">Editor</option>
        </select>
        <button type="submit" className="btn primary" disabled={!target || props.busy}>Share</button>
      </div>
      {q.trim() && !picked && !found.length && !looksLikeEmail(q) && (
        <p className="hint">Nobody matches. Type a full email to invite someone who hasn't signed in yet.</p>
      )}
    </form>
  );
}

function RequestRow(props: {
  name: string;
  asked: Role;
  message: string;
  at: number;
  busy: boolean;
  onApprove: (role: Role) => void;
  onDismiss: () => void;
}) {
  const [role, setRole] = useState<Role>(props.asked);
  return (
    <li className="share-request">
      <span className="share-who">
        {props.name}
        <span className="hub-muted"> asked for {props.asked === "editor" ? "edit" : "view"} access {ago(props.at)}</span>
        {props.message && <span className="share-message">“{props.message}”</span>}
      </span>
      <select aria-label={`Role to give ${props.name}`} value={role} onChange={(e) => setRole(e.target.value as Role)}>
        <option value="viewer">Viewer</option>
        <option value="editor">Editor</option>
      </select>
      <button className="btn sm primary" disabled={props.busy} onClick={() => props.onApprove(role)}>Approve</button>
      <button className="btn sm ghost" disabled={props.busy} onClick={props.onDismiss}>Dismiss</button>
    </li>
  );
}

function Links(props: {
  links: NonNullable<Sharing["links"]>;
  busy: boolean;
  made: { url: string; name: string } | null;
  onCreate: (name: string, days: number | null) => void;
  onRevoke: (id: string) => void;
}) {
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState(0);
  const [copied, setCopied] = useState(false);
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* the field is selectable: copy by hand */
    }
  };
  return (
    <section className="share-section">
      <h4>Share links</h4>
      <p className="hint">Anyone with a link can open this project read-only, without signing in. Revoke a link to close it.</p>
      {props.made && (
        <div className="share-made" role="status">
          <input readOnly className="mono" value={props.made.url} aria-label="The new link" onFocus={(e) => e.currentTarget.select()} />
          <button className="btn sm" onClick={() => copy(props.made!.url)}>{copied ? "Copied" : "Copy"}</button>
          <span className="hint">Shown once: copy it now.</span>
        </div>
      )}
      {props.links.length > 0 && (
        <ul className="share-list">
          {props.links.map((l) => (
            <li key={l.id} className={l.expired ? "disabled" : ""}>
              <span className="share-who">
                {l.name} <span className="mono hub-muted">…{l.hint}</span>
                <span className="hub-muted">
                  {" · "}
                  {l.expired ? "expired" : l.expires_at ? `expires ${new Date(l.expires_at * 1000).toLocaleDateString()}` : "no expiry"}
                  {" · "}
                  {l.last_used ? `opened ${ago(l.last_used)}` : "never opened"}
                </span>
              </span>
              <button className="btn sm ghost danger" disabled={props.busy} onClick={() => props.onRevoke(l.id)}>Revoke</button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="share-add-row"
        onSubmit={(e) => {
          e.preventDefault();
          props.onCreate(name.trim(), EXPIRY[expiry].days);
          setName("");
        }}
      >
        <input aria-label="Link name" placeholder="What it's for (e.g. the board)" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
        <select aria-label="Link expiry" value={expiry} onChange={(e) => setExpiry(Number(e.target.value))}>
          {EXPIRY.map((x, i) => (
            <option key={x.label} value={i}>{x.label}</option>
          ))}
        </select>
        <button type="submit" className="btn" disabled={props.busy}>Create link</button>
      </form>
    </section>
  );
}
