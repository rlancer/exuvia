import { useEffect } from "react";
import { useToast } from "@astryxdesign/core/Toast";

// Errors are raised from anywhere (a promise's catch, outside React); the
// page's one <Toasts /> shows them with Astryx's toast.
let show: ((text: string) => void) | null = null;
const waiting: string[] = [];

/** Say that something the user asked for did not happen. */
export function notifyError(what: string, e?: unknown) {
  const why = e instanceof Error ? e.message : e ? String(e) : "";
  const text = why ? `${what}: ${why}` : what;
  if (show) show(text);
  else waiting.push(text);
}

/** For a button's promise: `api.runStale().catch(failed("Couldn't start the run"))`. */
export const failed = (what: string) => (e: unknown) => notifyError(what, e);

/** Shows what `notifyError` raises. Mounted once per page. */
export function Toasts() {
  const showToast = useToast();
  useEffect(() => {
    show = (text) => void showToast({ body: text, type: "error", isAutoHide: true, autoHideDuration: 8000 });
    waiting.splice(0).forEach(show);
    return () => {
      show = null;
    };
  }, [showToast]);
  return null;
}
