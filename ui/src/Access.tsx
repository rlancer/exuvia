import { createContext, useContext, useState } from "react";
import { HUB_PROJECT, sharingApi, type Access, type Role } from "./api";
import { Mark } from "./Brand";

/**
 * What this page may do in its project (exuvia/sharing.py). A viewer reads
 * everything but changes nothing: the workbench hides what would change the
 * project, and the server turns such requests away regardless.
 */
export interface AccessState {
  role: Role;
  canEdit: boolean;
  canManage: boolean;
  access: Access | null;
}

export const FULL_ACCESS: AccessState = { role: "owner", canEdit: true, canManage: true, access: null };

export const AccessContext = createContext<AccessState>(FULL_ACCESS);

export const useAccess = () => useContext(AccessContext);

export function accessState(a: Access | null): AccessState {
  const role = a?.role ?? "owner";
  return { role, canEdit: role !== "viewer", canManage: role === "owner", access: a };
}

const ownerName = (a: Access | null) => a?.owner?.name ?? "its owner";

/** A strip under the top bar: this page can only look, and how to get more. */
export function ViewOnlyBanner() {
  const { access } = useAccess();
  const [sent, setSent] = useState(!!access?.requested);
  const [err, setErr] = useState<string | null>(null);
  if (!access) return null;
  const ask = async () => {
    setErr(null);
    try {
      await sharingApi.request(HUB_PROJECT!, { role: "editor" });
      setSent(true);
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <div className="view-only" role="status">
      <strong>View only.</strong>{" "}
      {access.link
        ? "You opened this project with a share link: you can look at its graph, data and views, but not change them."
        : `You can look at this project but not change it.`}
      {access.can_request && (
        sent ? (
          <span className="hub-muted"> Edit access requested from {ownerName(access)}.</span>
        ) : (
          <button className="btn sm" onClick={ask}>Request edit access</button>
        )
      )}
      {err && <span className="form-err"> {err}</span>}
    </div>
  );
}

/** The page for a project this person can't open: who to ask, and a button to ask. */
export function NoAccess(props: { access: Access | null; error?: string }) {
  const a = props.access;
  const [role, setRole] = useState<Role>("viewer");
  const [message, setMessage] = useState("");
  const [sent, setSent] = useState(!!a?.requested);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await sharingApi.request(HUB_PROJECT!, { role, message: message.trim() || undefined });
      setSent(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="hub">
      <main className="hub-main">
        <form className="hub-card" onSubmit={submit}>
          <Mark size={40} />
          <h2>You need access</h2>
          <p>
            {props.error ??
              (a?.user
                ? `You're signed in as ${a.user.id}, and project ${HUB_PROJECT} isn't shared with you.`
                : `Project ${HUB_PROJECT} isn't shared with you.`)}
          </p>
          {a?.can_request && !sent && (
            <>
              <div className="field">
                <label htmlFor="ask-role">Ask for</label>
                <select id="ask-role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
                  <option value="viewer">View access: read its code, data and views</option>
                  <option value="editor">Edit access: also change code and run nodes</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="ask-msg">Message (optional)</label>
                <textarea id="ask-msg" rows={3} maxLength={500} value={message} onChange={(e) => setMessage(e.target.value)} placeholder="What you need it for" />
              </div>
            </>
          )}
          {sent && <p className="hub-muted" role="status">Request sent. The owner sees it in the project's Share dialog.</p>}
          {err && <div className="form-err" role="alert">{err}</div>}
          <div className="actions">
            <a className="btn ghost" href="/">All projects</a>
            {a?.can_request && !sent && (
              <button type="submit" className="btn primary" disabled={busy}>Request access</button>
            )}
          </div>
        </form>
      </main>
    </div>
  );
}
