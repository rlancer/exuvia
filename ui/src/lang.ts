import type { Extension } from "@uiw/react-codemirror";
import { css } from "@codemirror/lang-css";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { sql } from "@codemirror/lang-sql";

/** A code file's editor language, by extension (none for TOML, Markdown, …). */
export function language(path: string): Extension[] {
  const ext = path.slice(path.lastIndexOf(".") + 1).toLowerCase();
  if (["tsx", "ts", "jsx", "js"].includes(ext)) return [javascript({ jsx: ext.endsWith("x"), typescript: ext.startsWith("t") })];
  if (ext === "py") return [python()];
  if (ext === "sql") return [sql()];
  if (ext === "css") return [css()];
  if (ext === "json") return [json()];
  return [];
}
