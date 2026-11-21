import { useState } from "react";

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
    <div className="code-copy">
      <pre className={props.wrap ? "wrap" : ""}>
        {props.wrap
          ? props.text
          : // Lines break only at spaces: a browser would also break after `-`, splitting `--url`.
            props.text.split(/( )/).map((part, i) => (part === " " ? " " : <span key={i} className="tok">{part}</span>))}
      </pre>
      <button type="button" className="btn sm" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
    </div>
  );
}
