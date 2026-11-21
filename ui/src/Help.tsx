import { useCallback, useEffect, useRef, useState } from "react";
import { about as fetchAbout, type About } from "./api";
import { Code } from "./Code";
import { IconHelp } from "./icons";
import { ago } from "./util";

const SEEN_KEY = "exuvia-update-seen";

function seenUpdate(): string | null {
  try {
    return localStorage.getItem(SEEN_KEY);
  } catch {
    return null; // private mode
  }
}

function markSeen(version: string) {
  try {
    localStorage.setItem(SEEN_KEY, version);
  } catch {
    /* private mode */
  }
}

const INSTALL: Record<About["install"], string> = {
  deployed: "Deployed server",
  source: "Development install from a clone",
  uvx: "Run with uvx",
  "uv-tool": "Installed with uv tool",
  pipx: "Installed with pipx",
  pip: "Installed with pip",
};

/**
 * The top bar's help button: the running version and whether a newer one is
 * out, plus links to the docs and the issue tracker. A new release is never
 * announced in a toast or a banner: the button gets a small dot until the
 * menu has been opened once for that release, and the menu says how to
 * update. What it says depends on the install: a command for someone's own
 * install, or, on a deployed server, that whoever runs it updates it.
 */
export function HelpMenu() {
  const [info, setInfo] = useState<About | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [checking, setChecking] = useState(false);
  const [seen, setSeen] = useState(seenUpdate);
  const wrap = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);

  const load = useCallback((refresh = false) => {
    setChecking(true);
    return fetchAbout(refresh)
      .then((a) => {
        setInfo(a);
        setFailed(false);
      })
      .catch(() => setFailed(true)) // an older server without /api/about
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const fresh = info?.update === "available" && info.latest ? info.latest : null;
  // Opening the menu is seeing the news: the dot stays away until the next release.
  useEffect(() => {
    if (open && fresh && seen !== fresh) {
      markSeen(fresh);
      setSeen(fresh);
    }
  }, [open, fresh, seen]);

  useEffect(() => {
    if (!open) return;
    // Capture phase: the graph canvas stops pointer events from bubbling.
    const onDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        button.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const dot = !!fresh && seen !== fresh;
  const label = dot ? `Help · exuvia ${fresh} is available` : "Help and version";

  return (
    <div className="help" ref={wrap}>
      <button
        ref={button}
        className="btn ghost icon-btn help-btn"
        onClick={() => setOpen((o) => !o)}
        title={label}
        aria-label={label}
        aria-expanded={open}
        aria-haspopup="dialog"
      >
        <IconHelp />
        {dot && <span className="help-dot" />}
      </button>
      {open && (
        <div className="help-pop" role="dialog" aria-label="Help">
          <div className="help-version">
            <span className="help-name">exuvia</span>
            <span className="mono">{info?.version ?? (failed ? "unknown" : "…")}</span>
          </div>
          {info && <div className="help-install">{INSTALL[info.install]}</div>}
          {info && <Update info={info} checking={checking} onCheck={() => load(true)} />}
          {failed && !info && <p className="help-muted">This server doesn't say which version it runs.</p>}
          <nav className="help-links">
            <a href={info?.docs_url ?? "https://exuvia.lobster.mp/"} target="_blank" rel="noreferrer">Documentation</a>
            <a href={info?.issues_url ?? "https://github.com/rlancer/exuvia/issues"} target="_blank" rel="noreferrer">Report an issue</a>
            <a href={info?.repo_url ?? "https://github.com/rlancer/exuvia"} target="_blank" rel="noreferrer">Source on GitHub</a>
          </nav>
        </div>
      )}
    </div>
  );
}

function Update(props: { info: About; checking: boolean; onCheck: () => void }) {
  const { info, checking, onCheck } = props;
  const again = (
    <button className="btn sm" onClick={onCheck} disabled={checking}>
      {checking ? "Checking…" : "Check again"}
    </button>
  );
  if (info.update === "off") {
    return <p className="help-muted">Update checks are off on this server.</p>;
  }
  if (info.update === "dev") {
    return <p className="help-muted">A development build: there is no release to compare it with.</p>;
  }
  if (info.update === "error") {
    return (
      <div className="help-update">
        <p className="help-muted">Couldn't check for a newer version{info.error ? `: ${info.error}` : ""}.</p>
        {again}
      </div>
    );
  }
  if (info.update === "current") {
    return (
      <div className="help-update help-row">
        <span className="help-ok">
          <span className="dot s-fresh" /> Up to date
          {info.checked_at ? <span className="help-muted"> · checked {ago(info.checked_at)}</span> : null}
        </span>
        {again}
      </div>
    );
  }
  // A newer release is out.
  return (
    <div className="help-update help-new">
      <p>
        <strong>exuvia {info.latest}</strong> is out.{" "}
        {info.changes_url && (
          <a href={info.changes_url} target="_blank" rel="noreferrer">
            What changed
          </a>
        )}
      </p>
      {info.upgrade ? (
        <>
          <p className="help-muted">To update, run this, then start exuvia again:</p>
          <Code text={info.upgrade} />
        </>
      ) : (
        <p className="help-muted">
          Whoever runs this server updates it by deploying the new image,{" "}
          <span className="mono">
            {info.image}:{info.latest}
          </span>
          . Nothing to install on your side.
        </p>
      )}
    </div>
  );
}
