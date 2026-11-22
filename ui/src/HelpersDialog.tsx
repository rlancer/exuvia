import { useEffect, useState } from "react";
import { api, type HelperFile } from "./api";
import { Dialog } from "./Dialog";

export function HelpersDialog({ onClose }: { onClose: () => void }) {
  const [files, setFiles] = useState<HelperFile[] | null>(null);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api.helpers().then(({ files }) => {
      if (active) setFiles(files);
    }).catch((error: Error) => {
      if (active) setError(error.message);
    });
    return () => { active = false; };
  }, []);

  const path = selected || files?.[0]?.path;
  return (
    <Dialog title="Shared Python helpers" onClose={onClose} className="wide">
      {error ? <p role="alert">Could not list helpers. {error}</p>
        : !files ? <p role="status">Loading helpers…</p>
        : !path ? <p>No Python helpers in <code>lib/</code>.</p>
        : <>
          <label className="field">
            <span>Helper file</span>
            <select value={path} onChange={(event) => setSelected(event.target.value)} autoFocus>
              {files.map((file) => <option key={file.path} value={file.path}>{file.path}</option>)}
            </select>
          </label>
          <HelperSource key={path} path={path} />
        </>}
    </Dialog>
  );
}

function HelperSource({ path }: { path: string }) {
  const [file, setFile] = useState<Awaited<ReturnType<typeof api.helper>> | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api.helper(path).then((file) => {
      if (active) setFile(file);
    }).catch((error: Error) => {
      if (active) setError(error.message);
    });
    return () => { active = false; };
  }, [path]);

  if (error) return <p role="alert">Could not read the helper. {error}</p>;
  if (!file) return <p role="status">Loading source…</p>;
  return (
    <>
      <p className="hub-muted">
        {file.used_by.length ? <>Used by <span className="mono">{file.used_by.join(", ")}</span></> : "No nodes import this helper."}
      </p>
      <pre className="helper-source mono" tabIndex={0} aria-label={`Source of ${file.path}`}>
        <code>{file.source}</code>
      </pre>
    </>
  );
}
