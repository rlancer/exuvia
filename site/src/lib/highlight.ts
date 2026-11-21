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

/**
 * The copy button: two sheets of paper, and the tick that takes their place
 * once the text is on the clipboard. It copies `text`, or without one the
 * `pre` of the `data-code` element it sits in (the script is in Base.astro).
 */
export function copyButton(text = ""): string {
  const icon = (name: string, path: string) =>
    `<svg class="${name}" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">${path}</svg>`;
  return (
    `<button class="copy" type="button" data-copy="${escapeHtml(text)}" aria-label="Copy" title="Copy">` +
    icon("copy-icon", `<path d="M5.5 5.5h8v8h-8z"/><path d="M10.5 5.5v-3h-8v8h3"/>`) +
    icon("copy-done", `<path d="M3 8.6l3.1 3.1L13 4.6"/>`) +
    `<span class="copy-note" role="status"></span></button>`
  );
}

/** A complete code figure: optional file name bar, copy button, highlighted body. */
export function codeFigure(h: Highlighter, code: string, lang?: string, file?: string): string {
  const body = highlight(h, code.replace(/\n+$/, ""), lang);
  const copy = copyButton();
  const bar = file ? `<div class="code-file"><span class="mono">${escapeHtml(file)}</span>${copy}</div>` : copy;
  return `<div class="code" data-code>${bar}${body}</div>`;
}
