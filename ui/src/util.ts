export function ago(ts?: number | null): string {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 5) return "just now";
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function until(ts?: number | null): string {
  if (!ts) return "";
  const s = ts - Date.now() / 1000;
  if (s <= 0) return "due";
  if (s < 60) return `in ${Math.ceil(s)}s`;
  if (s < 3600) return `in ${Math.round(s / 60)}m`;
  if (s < 86400) return `in ${Math.round(s / 3600)}h`;
  return `in ${Math.round(s / 86400)}d`;
}

export function dur(a?: number | null, b?: number | null): string {
  if (!a || !b) return "";
  const s = b - a;
  return s < 1 ? `${Math.round(s * 1000)}ms` : s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

export function fmtNum(n?: number | null): string {
  return n == null ? "" : n.toLocaleString();
}

export function fmtCell(v: unknown): string {
  if (v == null) return "∅";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return v.toLocaleString();
    const a = Math.abs(v);
    return a !== 0 && (a < 1e-3 || a >= 1e9) ? v.toExponential(3) : v.toLocaleString(undefined, { maximumFractionDigits: 6 });
  }
  if (typeof v === "string" && /^\d{4}-\d\d-\d\dT00:00:00$/.test(v)) return v.slice(0, 10);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** Props that make a row act as a button: it takes the focus, and Enter or Space press it. */
export function pressable(fn: () => void) {
  return {
    tabIndex: 0,
    role: "button",
    onClick: fn,
    onKeyDown: (e: { key: string; preventDefault: () => void }) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        fn();
      }
    },
  } as const;
}
