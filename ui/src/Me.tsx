import { useCallback, useEffect, useRef, useState } from "react";
import { HUB_PROJECT, usersApi, type UserRow } from "./api";
import { ConnectDialog } from "./Connect";
import { ago } from "./util";
import { Dialog } from "./Dialog";
import { IconUser } from "./icons";
import { useMe } from "./useMe";

/**
 * The signed-in person's account menu (EXUVIA_AUTH=headers), at the end of
 * the top bar: a profile icon that opens who they are and what they can do
 * from here. Connect an agent (on a hub: their own agent token, with the MCP
 * setup around it) and, for an admin, Users, which manages the user list.
 * `hub`: this page is the hub's home; a hub project's workbench is one too.
 */
export function SignedInAs(props: { hub?: boolean }) {
  const who = useMe()?.user ?? null;
  const [open, setOpen] = useState(false);
  const [dialog, setDialog] = useState<"users" | "connect" | null>(null);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => root.current && !root.current.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  if (!who) return null;
  // The connect setup is for a whole hub; someone in by agent token is an agent.
  const canConnect = (props.hub || !!HUB_PROJECT) && who.method !== "token";
  const pick = (d: "users" | "connect") => {
    setOpen(false);
    setDialog(d);
  };
  return (
    <div className="account" ref={root}>
      <button
        className="btn ghost icon-btn account-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account"
        title={who.email && who.email !== who.id ? `${who.id} · ${who.email}` : who.id}
        onClick={() => setOpen((o) => !o)}
      >
        <IconUser size={16} />
      </button>
      {open && (
        <div className="account-menu" role="menu">
          <div className="account-who">
            <div className="account-name">{who.id}</div>
            {who.email && who.email !== who.id && <div className="account-email">{who.email}</div>}
            {who.role === "admin" && <div className="account-role">admin</div>}
          </div>
          {canConnect && (
            <button role="menuitem" onClick={() => pick("connect")}>
              Connect an agent…
              <span>Your agent token and the MCP setup</span>
            </button>
          )}
          {who.role === "admin" && (
            <button role="menuitem" onClick={() => pick("users")}>
              Users…
              <span>Roles and disabled accounts</span>
            </button>
          )}
        </div>
      )}
      {dialog === "users" && <UsersDialog self={who.uid} onClose={() => setDialog(null)} />}
      {dialog === "connect" && <ConnectDialog onClose={() => setDialog(null)} />}
    </div>
  );
}

function UsersDialog(props: { self: string | null; onClose: () => void }) {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => usersApi.list().then(setUsers).catch((e: Error) => setErr(e.message)), []);
  useEffect(() => {
    load();
  }, [load]);
  const change = async (u: UserRow, c: { role?: "admin" | "member"; disabled?: boolean }) => {
    setErr(null);
    try {
      await usersApi.update(u.id, c);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <Dialog title="Users" className="users-dialog" onClose={props.onClose}>
        <p className="hint">
          Everyone who has signed in. A disabled user is turned away even when the sign-in proxy lets them in.
          People named in <span className="mono">EXUVIA_ADMINS</span> are always admins.
        </p>
        {err && <div className="err-banner"><div>{err}</div></div>}
        {!users ? (
          <div className="placeholder">Loading…</div>
        ) : (
          <div className="users-scroll">
            <table className="grid users">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Role</th>
                  <th>Last seen</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const self = u.id === props.self;
                  const locked = u.pinned_admin || self;
                  return (
                    <tr key={u.id} className={u.disabled ? "disabled" : ""}>
                      <td title={u.identities.map((i) => `${i.method}: ${i.subject}`).join("\n")}>
                        {u.name}
                        {self ? " (you)" : ""}
                      </td>
                      <td>{u.email ?? ""}</td>
                      <td>
                        <select
                          value={u.role}
                          aria-label={`Role of ${u.name}`}
                          disabled={locked}
                          title={u.pinned_admin ? "Named in EXUVIA_ADMINS" : self ? "Another admin can change your role" : ""}
                          onChange={(e) => change(u, { role: e.target.value as "admin" | "member" })}
                        >
                          <option value="member">member</option>
                          <option value="admin">admin</option>
                        </select>
                      </td>
                      <td title={new Date(u.last_seen * 1000).toLocaleString()}>{ago(u.last_seen)}</td>
                      <td>
                        <button
                          className="btn sm"
                          disabled={locked && !u.disabled}
                          onClick={() => change(u, { disabled: !u.disabled })}
                        >
                          {u.disabled ? "Enable" : "Disable"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="actions">
          <button className="btn" onClick={props.onClose}>Close</button>
        </div>
    </Dialog>
  );
}
