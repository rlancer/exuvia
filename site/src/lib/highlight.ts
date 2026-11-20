import { createCssVariablesTheme, createHighlighter, type Highlighter } from "shiki";

// Token colours come from CSS variables on `.code` (src/styles/global.css),
// so code blocks follow the site palette instead of a bundled editor theme.
const theme = createCssVariablesTheme({ name: "exuvia", variablePrefix: "--shiki-", fontStyle: true });

const LANGS = ["bash", "python", "sql", "json", "tsx", "css", "javascript", "dockerfile", "toml"] as const;
const ALIASES: Record<string, string> = { js: "javascript", py: "python", sh: "bash", shell: "bash", ts: "tsx" };

let highlighter: Promise<Highlighter> | undefined;

export function getHighlighter(): Promise<Highlighter> {
  highlighter ??= createHighlighter({ themes: [theme], langs: [...LANGS] });
  return highlighter;
}

export function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** Highlighted `<pre>` for a code string. Unknown or missing languages render as plain text. */
export function highlight(h: Highlighter, code: string, lang?: string): string {
  const name = ALIASES[lang ?? ""] ?? lang ?? "";
  if (!(LANGS as readonly string[]).includes(name)) {
    return `<pre tabindex="0"><code>${escapeHtml(code)}</code></pre>`;
  }
  return h.codeToHtml(code, { lang: name, theme: "exuvia" });
}

/** A complete code figure: optional file name bar, copy button, highlighted body. */
export function codeFigure(h: Highlighter, code: string, lang?: string, file?: string): string {
  const body = highlight(h, code.replace(/\n+$/, ""), lang);
  const copy = `<button class="copy" type="button" data-copy>Copy</button>`;
  const bar = file ? `<div class="code-file"><span class="mono">${escapeHtml(file)}</span>${copy}</div>` : copy;
  return `<div class="code" data-code>${bar}${body}</div>`;
}
