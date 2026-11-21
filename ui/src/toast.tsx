import { useEffect, useState } from "react";
import { IconClose } from "./icons";

type Toast = { id: number; text: string };

let toasts: Toast[] = [];
let seq = 0;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

function dismiss(id: number) {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

/** Say that something the user asked for did not happen. */
export function notifyError(what: string, e?: unknown) {
  const why = e instanceof Error ? e.message : e ? String(e) : "";
  const t = { id: ++seq, text: why ? `${what}: ${why}` : what };
  toasts = [...toasts.slice(-3), t];
  emit();
  setTimeout(() => dismiss(t.id), 8000);
}

/** For a button's promise: `api.runStale().catch(failed("Couldn't start the run"))`. */
export const failed = (what: string) => (e: unknown) => notifyError(what, e);

/** The stack of messages, bottom centre. Mounted once per page. */
export function Toasts() {
  const [, tick] = useState(0);
  useEffect(() => {
    const on = () => tick((n) => n + 1);
    listeners.add(on);
    return () => void listeners.delete(on);
  }, []);
  if (!toasts.length) return null;
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className="toast">
          <span>{t.text}</span>
          <button className="btn ghost icon-btn sm" onClick={() => dismiss(t.id)} aria-label="Dismiss">
            <IconClose size={12} />
          </button>
        </div>
      ))}
    </div>
  );
}
