import CodeMirror, { EditorView, Prec, type Extension, type ReactCodeMirrorProps } from "@uiw/react-codemirror";
import * as stylex from "@stylexjs/stylex";

/**
 * The editor's skin, in CodeMirror's own theme API: the app's code ground,
 * its mono, and the theme's rules and text. The values are the theme's
 * custom properties, so the editor follows the colour mode with the page.
 * Highest precedence: CodeMirror's own dark theme sets a ground too.
 */
const skin = Prec.highest(EditorView.theme({
  "&": { height: "100%", fontSize: "var(--text-code-size)", backgroundColor: "var(--exuvia-code-background)" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--font-family-code)", lineHeight: "1.5" },
  ".cm-gutters": {
    backgroundColor: "var(--exuvia-code-background)",
    borderRight: "1px solid var(--color-border)",
    color: "var(--color-text-disabled)",
  },
  // A saved version shown as a diff (History.tsx): what was added, what went, what is folded away.
  ".cm-changedLine": { backgroundColor: "color-mix(in srgb, var(--color-success) 12%, transparent) !important" },
  ".cm-deletedChunk": { backgroundColor: "color-mix(in srgb, var(--color-error) 12%, transparent) !important" },
  ".cm-collapsedLines": { backgroundColor: "var(--color-background-muted)", color: "var(--color-text-secondary)" },
}));

const styles = stylex.create({
  editor: { flexGrow: 1, minHeight: 0, minWidth: 0, overflow: "auto" },
});

/**
 * Every code editor in the app: CodeMirror in the app's skin, filling the
 * column it is in (its parent is a flex column with a height). Lines wrap.
 * `extensions` adds the language (lang.ts) and anything else.
 */
export function CodeEditor(
  props: Omit<ReactCodeMirrorProps, "theme" | "height" | "style" | "className" | "extensions"> & {
    dark: boolean;
    extensions?: Extension[];
    /** Highlight the line the cursor is on (an editor) or not (a read-only view). */
    activeLine?: boolean;
  },
) {
  const { dark, extensions = [], activeLine = true, ...rest } = props;
  return (
    <CodeMirror
      {...rest}
      {...stylex.props(styles.editor)}
      theme={dark ? "dark" : "light"}
      height="100%"
      extensions={[...extensions, EditorView.lineWrapping, skin]}
      basicSetup={{ foldGutter: false, highlightActiveLine: activeLine }}
    />
  );
}
