import { useState } from "react";
import { api, type GraphNode } from "./api";
import { Dialog } from "./Dialog";

export function NewNodeDialog(props: {
  nodes: GraphNode[];
  initialDeps: string[];
  onClose: () => void;
  onCreated: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"python" | "sql">("python");
  const [deps, setDeps] = useState<string[]>(props.initialDeps);
  const [description, setDescription] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const taken = props.nodes.some((n) => n.name === name);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await api.create({ name, kind, deps, description });
      props.onCreated(name);
    } catch (e) {
      setBusy(false);
      setErr((e as Error).message);
    }
  };

  return (
    <Dialog title="New node" onClose={props.onClose} onSubmit={submit}>
        <div className="field">
          <label htmlFor="nn-name">Name</label>
          <input
            id="nn-name"
            autoFocus
            spellCheck={false}
            className="mono"
            value={name}
            placeholder="rolling_corr"
            onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))}
          />
          <span className="hint">
            The file is <span className="mono">nodes/{name || "name"}.{kind === "sql" ? "sql" : "py"}</span>; other nodes read it by this name.
          </span>
        </div>
        <div className="field">
          <label id="nn-kind">Kind</label>
          <div className="seg" role="radiogroup" aria-labelledby="nn-kind" style={{ alignSelf: "flex-start" }}>
            {(["python", "sql"] as const).map((k) => (
              <button type="button" role="radio" aria-checked={kind === k} key={k} className={kind === k ? "on" : ""} onClick={() => setKind(k)}>
                {k === "python" ? "Python" : "SQL"}
              </button>
            ))}
          </div>
        </div>
        {props.nodes.length > 0 && (
          <div className="field">
            <label id="nn-deps">Inputs (upstream nodes)</label>
            <div className="deps" role="group" aria-labelledby="nn-deps">
              {props.nodes.map((n) => (
                <label key={n.name} className={deps.includes(n.name) ? "on" : ""}>
                  <input
                    type="checkbox"
                    checked={deps.includes(n.name)}
                    onChange={(e) => setDeps((d) => (e.target.checked ? [...d, n.name] : d.filter((x) => x !== n.name)))}
                  />
                  {n.name}
                </label>
              ))}
            </div>
          </div>
        )}
        <div className="field">
          <label htmlFor="nn-desc">Description</label>
          <input id="nn-desc" value={description} placeholder="What this node produces" onChange={(e) => setDescription(e.target.value)} />
        </div>
        {(taken || err) && <div className="form-err" role="alert">{taken ? `There is already a node ${name}.` : err}</div>}
        <div className="actions">
          <button type="button" className="btn ghost" onClick={props.onClose}>Cancel</button>
          <button type="submit" className="btn primary" disabled={!name || taken || busy}>Create</button>
        </div>
    </Dialog>
  );
}
