import { useEffect, useState } from "react";
import { api, type SecretRow } from "./api";
import { Dialog } from "./Dialog";

const NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

const when = (at: number | null) => (at ? new Date(at * 1000).toLocaleString() : "");

/** The project's secrets: names only. A value can be set or replaced, never read back. */
export function SecretsDialog({ onClose }: { onClose: () => void }) {
  const [rows, setRows] = useState<SecretRow[] | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api.secrets().then(({ secrets }) => {
      if (active) setRows(secrets);
    }).catch((e: Error) => {
      if (active) setErr(e.message);
    });
    return () => { active = false; };
  }, []);

  const act = async (label: string, fn: () => Promise<{ secrets: SecretRow[] }>) => {
    setBusy(label);
    setErr(null);
    try {
      setRows((await fn()).secrets);
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(null);
    }
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (await act("set", () => api.secretSet(name.trim(), value))) {
      setName("");
      setValue("");
    }
  };

  const valid = NAME.test(name.trim()) && value.length > 0;
  return (
    <Dialog title="Secrets" className="wide packages" onClose={onClose}>
      <p className="pk-note">
        Credentials this project's nodes read as environment variables, e.g. <code>os.environ["SHOP_API_KEY"]</code>.
        They are kept outside the project's files, so code history and exports never carry them. Values can be
        replaced but not read back, and are shown as <code>***</code> in run logs and to the agent.
      </p>

      <form className="pk-add" onSubmit={save}>
        <input
          className="mono"
          autoFocus
          spellCheck={false}
          autoComplete="off"
          aria-label="Secret name"
          placeholder="NAME"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <input
          className="mono"
          type="password"
          autoComplete="new-password"
          aria-label="Secret value"
          placeholder="value"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <button type="submit" className="btn primary" disabled={!valid || !!busy}>
          {busy === "set" ? "Saving…" : rows?.some((r) => r.name === name.trim()) ? "Replace" : "Add"}
        </button>
      </form>
      {err && <pre className="pk-error" role="alert">{err}</pre>}

      {!rows ? (
        !err && <p role="status">Loading secrets…</p>
      ) : rows.length === 0 ? (
        <p className="pk-note">No secrets yet.</p>
      ) : (
        <table className="pk-table">
          <tbody>
            {rows.map((r) => (
              <tr key={r.name}>
                <td className="mono">{r.name}</td>
                <td className="pk-source">{r.updated_at ? `set ${when(r.updated_at)}` : ""}</td>
                <td>
                  <button
                    className="btn sm ghost"
                    title={`Replace the value of ${r.name}`}
                    disabled={!!busy}
                    onClick={() => setName(r.name)}
                  >
                    Replace
                  </button>
                  <button
                    className="btn sm ghost danger"
                    title={`Delete ${r.name}`}
                    disabled={!!busy}
                    onClick={() => act(`rm${r.name}`, () => api.secretDelete(r.name))}
                  >
                    {busy === `rm${r.name}` ? "…" : "Delete"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Dialog>
  );
}
