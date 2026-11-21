import { useState } from "react";
import { api, type PackagesState } from "./api";

const STATUS: Record<PackagesState["status"], string> = {
  none: "no packages listed",
  pending: "waiting to install",
  installing: "installing…",
  ready: "installed",
  failed: "install failed",
  remote: "installed by the executor",
};

const NEW_MANIFEST = '[project]\nname = "project"\nversion = "0"\nrequires-python = ">=3.11"\ndependencies = []\n';

/** Who made a request: the built-in agent, an API client, or an MCP client's own name. */
const requester = (origin: string) => (origin === "agent" ? "The agent" : origin === "api" ? "An API client" : origin);

/** Requirement strings typed into the add box: `scipy, acme-models>=2` or one per line. */
const parseSpecs = (text: string) =>
  text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);

/** The project's packages: what pyproject.toml lists, adding and removing,
 * the install status and log, and the agent's requests to approve. */
export function PackagesDialog(props: { state: PackagesState | null; onChange: (s: PackagesState) => void; onClose: () => void }) {
  const { state, onChange, onClose } = props;
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [showLog, setShowLog] = useState(false);

  const act = async (label: string, fn: () => Promise<PackagesState>) => {
    setBusy(label);
    setErr(null);
    try {
      onChange(await fn());
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(null);
    }
  };

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    const specs = parseSpecs(adding);
    if (specs.length && (await act("add", () => api.packagesEdit({ add: specs })))) setAdding("");
  };

  const status = state?.status ?? "none";
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog wide packages">
        <div className="pk-head">
          <h3>Packages</h3>
          <span className={`pk-status pk-${status}`}>
            <span className="dot" /> {STATUS[status]}
            {status === "ready" && state?.installed ? ` · ${state.installed} on top of exuvia's own` : ""}
          </span>
          <span className="spacer" />
          <button className="btn ghost icon-btn" onClick={onClose} title="Close">✕</button>
        </div>
        <p className="pk-note">
          The packages your nodes import beyond those exuvia comes with, from the project's <code>pyproject.toml</code>.
          A package added without a version is pinned to the one installed.
        </p>

        {state?.requests.map((r) => (
          <div key={r.id} className="pk-request">
            <div>
              <b>{requester(r.origin)} asks for</b> <code>{r.packages.join(", ")}</code>
              {r.reason && <div className="pk-reason">{r.reason}</div>}
            </div>
            <button className="btn sm primary" disabled={!!busy} onClick={() => act(`req${r.id}`, () => api.packagesApprove(r.id))}>
              {busy === `req${r.id}` ? "Checking…" : "Install"}
            </button>
            <button className="btn sm ghost" disabled={!!busy} onClick={() => act(`req${r.id}`, () => api.packagesDismiss(r.id))}>
              Dismiss
            </button>
          </div>
        ))}

        <form className="pk-add" onSubmit={add}>
          <input
            className="mono"
            autoFocus
            value={adding}
            placeholder="scikit-learn, acme-models>=2"
            onChange={(e) => setAdding(e.target.value)}
          />
          <button type="submit" className="btn primary" disabled={!parseSpecs(adding).length || !!busy}>
            {busy === "add" ? "Checking…" : "Add"}
          </button>
        </form>
        {err && <pre className="pk-error">{err}</pre>}

        {state && state.packages.length > 0 && (
          <table className="pk-table">
            <tbody>
              {state.packages.map((p) => (
                <tr key={p.requirement}>
                  <td className="mono">{p.requirement}</td>
                  <td className="mono pk-version">{p.version ?? "—"}</td>
                  <td className="pk-source">{p.source === "exuvia" ? "comes with exuvia" : p.source === "project" ? "installed" : ""}</td>
                  <td>
                    <button
                      className="btn sm ghost danger"
                      title={`Remove ${p.name}`}
                      disabled={!!busy}
                      onClick={() => act(`rm${p.name}`, () => api.packagesEdit({ remove: [p.name] }))}
                    >
                      {busy === `rm${p.name}` ? "…" : "Remove"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {status === "failed" && state?.error && (
          <div className="pk-failed">
            <pre className="pk-error">{state.error}</pre>
            <button className="btn sm" disabled={!!busy} onClick={() => act("retry", api.packagesRetry)}>Retry</button>
          </div>
        )}

        <div className="pk-links">
          <button className="btn sm ghost" onClick={() => setEditing(editing === null ? state?.manifest ?? NEW_MANIFEST : null)}>
            {editing === null ? "Edit pyproject.toml" : "Close editor"}
          </button>
          {state?.log && (
            <button className="btn sm ghost" onClick={() => setShowLog((v) => !v)}>{showLog ? "Hide" : "Show"} install log</button>
          )}
          <span className="spacer" />
          <span className="pk-agent">
            {state?.agent === "install" ? "The agent installs packages itself" : state?.agent === "off" ? "The agent cannot ask for packages" : "Agent requests wait for your approval"}
          </span>
        </div>
        {editing !== null && (
          <div className="pk-editor">
            <textarea className="mono" spellCheck={false} value={editing} onChange={(e) => setEditing(e.target.value)} />
            <div className="actions">
              <button
                className="btn primary sm"
                disabled={!!busy}
                onClick={async () => {
                  if (await act("manifest", () => api.packagesManifest(editing))) setEditing(null);
                }}
              >
                Save and install
              </button>
            </div>
          </div>
        )}
        {showLog && state?.log && <pre className="log pk-log">{state.log}</pre>}
      </div>
    </div>
  );
}
