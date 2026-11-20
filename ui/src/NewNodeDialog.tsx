import { useState } from "react";
import { api, type GraphNode } from "./api";

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

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    try {
      await api.create({ name, kind, deps, description });
      props.onCreated(name);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && props.onClose()}>
      <form className="dialog" onSubmit={submit}>
        <h3>New node</h3>
        <div className="field">
          <label>Name</label>
          <input
            autoFocus
            className="mono"
            value={name}
            placeholder="rolling_corr"
            onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))}
          />
        </div>
        <div className="field">
          <label>Kind</label>
          <div className="seg">
            {(["python", "sql"] as const).map((k) => (
              <button type="button" key={k} className={kind === k ? "on" : ""} onClick={() => setKind(k)}>{k}</button>
            ))}
          </div>
        </div>
        {props.nodes.length > 0 && (
          <div className="field">
            <label>Inputs (upstream nodes)</label>
            <div className="deps">
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
          <label>Description</label>
          <input value={description} placeholder="What this node produces" onChange={(e) => setDescription(e.target.value)} />
        </div>
        {err && <div className="form-err">{err}</div>}
        <div className="actions">
          <button type="button" className="btn ghost" onClick={props.onClose}>Cancel</button>
          <button type="submit" className="btn primary" disabled={!name}>Create</button>
        </div>
      </form>
    </div>
  );
}
