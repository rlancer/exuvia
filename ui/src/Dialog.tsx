import { useEffect, useId, useRef, type FormEvent, type ReactNode } from "react";
import { IconClose } from "./icons";

/** The open dialogs, innermost last: only it answers Escape and Tab (a confirmation over a dialog). */
const open: object[] = [];

const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

/**
 * Every dialog in the app: a panel over a scrim, with its title and a close
 * button. Escape and a click on the scrim close it, Tab stays inside it, and
 * the control that opened it has the focus again once it is closed. With
 * `onSubmit` the panel is a form.
 */
export function Dialog(props: {
  title: ReactNode;
  onClose: () => void;
  onSubmit?: (e: FormEvent) => void;
  className?: string;
  /** Next to the title, before the close button. */
  head?: ReactNode;
  children: ReactNode;
}) {
  const { onClose } = props;
  const id = useId();
  const panel = useRef<HTMLElement | null>(null);
  const close = useRef(onClose);
  close.current = onClose;
  // Read while rendering: by the time an effect runs, an `autoFocus` field has the focus.
  const opener = useRef(document.activeElement as HTMLElement | null);

  useEffect(() => {
    const me = {};
    open.push(me);
    const el = panel.current;
    // An `autoFocus` field has the focus already; otherwise take the first control.
    if (el && !el.contains(document.activeElement)) {
      (el.querySelector(".dialog-body")?.querySelector<HTMLElement>(FOCUSABLE) ?? el).focus();
    }
    const onKey = (e: KeyboardEvent) => {
      if (open[open.length - 1] !== me) return;
      if (e.key === "Escape") {
        e.stopPropagation();
        close.current();
      } else if (e.key === "Tab" && el) {
        const items = [...el.querySelectorAll<HTMLElement>(FOCUSABLE)];
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        const at = document.activeElement;
        if (e.shiftKey && (at === first || !el.contains(at))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && (at === last || !el.contains(at))) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      open.splice(open.indexOf(me), 1);
      opener.current?.focus?.();
    };
  }, []);

  const inner = (
    <>
      <div className="dialog-head">
        <h3 id={id}>{props.title}</h3>
        {props.head}
        <span className="spacer" />
        <button type="button" className="btn ghost icon-btn sm" onClick={onClose} aria-label="Close" title="Close (Esc)">
          <IconClose />
        </button>
      </div>
      <div className="dialog-body">{props.children}</div>
    </>
  );
  const common = {
    className: `dialog ${props.className ?? ""}`,
    role: "dialog",
    "aria-modal": true,
    "aria-labelledby": id,
    tabIndex: -1,
  } as const;

  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      {props.onSubmit ? (
        <form {...common} ref={(el) => (panel.current = el)} onSubmit={props.onSubmit}>{inner}</form>
      ) : (
        <div {...common} ref={(el) => (panel.current = el)}>{inner}</div>
      )}
    </div>
  );
}
