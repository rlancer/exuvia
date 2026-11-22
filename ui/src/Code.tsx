import { useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { borderVars, colorVars, radiusVars, sizeVars, spacingVars, typeScaleVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Button } from "@astryxdesign/core/Button";

const styles = stylex.create({
  // One block on the code ground: the text, and the button that copies it in its corner.
  block: {
    display: "flex",
    alignItems: "flex-start",
    gap: spacingVars["--spacing-2"],
    minWidth: 0,
    padding: spacingVars["--spacing-1"],
    paddingInlineStart: spacingVars["--spacing-3"],
    backgroundColor: "var(--exuvia-code-background)",
    borderWidth: borderVars["--border-width"],
    borderStyle: "solid",
    borderColor: colorVars["--color-border"],
    borderRadius: radiusVars["--radius-container"],
  },
  text: {
    flexGrow: 1,
    flexBasis: 0,
    minWidth: 0,
    margin: 0,
    // One line of text sits level with the button beside it.
    paddingBlock: `calc((${sizeVars["--size-element-sm"]} - 1lh) / 2)`,
    fontFamily: typographyVars["--font-family-code"],
    fontSize: typeScaleVars["--text-code-size"],
    lineHeight: 1.5,
    color: colorVars["--color-text-primary"],
    whiteSpace: "pre-wrap",
    overflowWrap: "break-word",
    fontVariantLigatures: "none",
  },
  // Prose to copy (a prompt): the body's face, and a height it scrolls in.
  prose: {
    fontFamily: typographyVars["--font-family-body"],
    fontSize: typeScaleVars["--text-body-size"],
    maxHeight: "240px",
    overflowY: "auto",
  },
  tok: { whiteSpace: "pre" },
  copy: { flexShrink: 0 },
});

/** A block of text to copy, with a button that copies it. */
export function Code(props: { text: string; wrap?: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(props.text);
    } catch {
      // No clipboard API (plain http on another host): copy through a selection.
      const t = document.createElement("textarea");
      t.value = props.text;
      document.body.appendChild(t);
      t.select();
      document.execCommand("copy");
      t.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div {...stylex.props(styles.block)}>
      <pre {...stylex.props(styles.text, props.wrap && styles.prose)}>
        {props.wrap
          ? props.text
          : // Lines break only at spaces: a browser would also break after `-`, splitting `--url`.
            props.text.split(/( )/).map((part, i) => (part === " " ? " " : <span key={i} {...stylex.props(styles.tok)}>{part}</span>))}
      </pre>
      <Button size="sm" label={copied ? "Copied" : "Copy"} onClick={copy} xstyle={styles.copy} />
    </div>
  );
}
