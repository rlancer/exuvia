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

/** Minimal, safe markdown: fences, inline code, bold, bullet/numbered lists, paragraphs. */
export function renderMarkdown(src: string): string {
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s: string) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const out: string[] = [];
  const parts = src.split(/```/);
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      out.push(`<pre><code>${esc(part.replace(/^[a-z]*\n/, ""))}</code></pre>`);
      return;
    }
    let list: "ul" | "ol" | null = null;
    let para: string[] = [];
    const flush = () => {
      if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`);
      para = [];
    };
    const close = () => {
      if (list) out.push(`</${list}>`);
      list = null;
    };
    for (const line of part.split("\n")) {
      const ul = line.match(/^\s*[-*]\s+(.*)/);
      const ol = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (ul || ol) {
        flush();
        const want = ul ? "ul" : "ol";
        if (list !== want) {
          close();
          out.push(`<${want}>`);
          list = want;
        }
        out.push(`<li>${inline((ul || ol)![1])}</li>`);
      } else if (!line.trim()) {
        flush();
        close();
      } else {
        close();
        para.push(line.replace(/^#+\s*/, ""));
      }
    }
    flush();
    close();
  });
  return out.join("");
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

/**
 * A Markdown text's first paragraph as one line of plain text: what a node's
 * card shows of its docstring, where there is no room to render it. The marks
 * go (`code`, **bold**, _emphasis_, [links](…), a heading's #, a list's -) and
 * their words stay; an underscore inside a name (rolling_vol) is not emphasis.
 */
export function summary(md: string): string {
  const lines: string[] = [];
  for (const raw of md.trim().split("\n")) {
    const line = raw.trim();
    if (line.startsWith("```") || line.startsWith("~~~")) {
      if (lines.length) break;
      continue;
    }
    if (!line) {
      if (lines.length) break;
      continue;
    }
    lines.push(line.replace(/^#{1,6}\s+/, "").replace(/^>\s?/, "").replace(/^([-*+]|\d+[.)])\s+/, ""));
  }
  // Code spans keep their text as written; the marks are taken off around them.
  return lines
    .join(" ")
    .split(/(`+[^`]*`+)/)
    .map((part, i) =>
      i % 2
        ? part.replace(/^`+|`+$/g, "")
        : part
            .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
            .replace(/(\*\*|__)(?=\S)(.+?)(?<=\S)\1/g, "$2")
            .replace(/(^|[^\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?!\w)/g, "$1$2")
            .replace(/(^|[^\w])_(?=\S)([^_]+?)(?<=\S)_(?!\w)/g, "$1$2")
            .replace(/~~(.+?)~~/g, "$1"),
    )
    .join("")
    .replace(/\s+/g, " ")
    .trim();
}
