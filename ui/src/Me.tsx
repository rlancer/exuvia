import { useCallback, useEffect, useState } from "react";
import { me, usersApi, type Me, type UserRow } from "./api";
import { ago } from "./util";

/** The person a sign-in proxy says this is (EXUVIA_AUTH=headers), for the
 * top bar. An admin also gets the Users button, which manages the user list. */
export function SignedInAs() {
  const [who, setWho] = useState<Me["user"]>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    me()
      .then((m) => setWho(m.user))
      .catch(() => setWho(null)); // an older server, or not signed in
  }, []);
  if (!who) return null;
  return (
    <>
      {who.role === "admin" && (
        <button className="btn" onClick={() => setOpen(true)} title="Everyone who has signed in: roles and disabled accounts">
          Users
        </button>
      )}
      <span className="signed-in" title={who.email && who.email !== who.id ? `${who.id} · ${who.email}` : who.id}>
        {who.id}
      </span>
      {open && <UsersDialog self={who.uid} onClose={() => setOpen(false)} />}
    </>
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
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && props.onClose()}>
      <div className="dialog users-dialog" role="dialog" aria-label="Users">
        <h3>Users</h3>
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
      </div>
    </div>
  );
}
