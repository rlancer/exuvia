import { lazy, Suspense } from "react";
import type { MarkdownComponents, MarkdownProps } from "@astryxdesign/core/Markdown";

/**
 * Astryx's Markdown renderer, fetched the first time something shows Markdown
 * (an agent's reply, a node's docstring in the notebook): a page with none
 * costs nothing. Render it inside a Suspense whose fallback is the plain text.
 */
const AstryxMarkdown = lazy(() => import("@astryxdesign/core/Markdown").then((m) => ({ default: m.Markdown })));

/** KaTeX, fetched only once a text actually has maths in it. */
const LazyMath = lazy(() => import("./Math"));

/**
 * `$…$` inline and `$$…$$` on lines of their own, typeset; until KaTeX has
 * loaded, the expression as written. Astryx decides what is maths: a price
 * ("$5 to $10"), a SQL parameter ("$as_of") and a code span stay text.
 */
const math: NonNullable<MarkdownComponents["math"]> = ({ value, display }) => (
  <Suspense fallback={display === "block" ? `$$${value}$$` : `$${value}$`}>
    <LazyMath value={value} display={display} />
  </Suspense>
);

/** Markdown as exuvia shows it everywhere: Astryx's, with maths typeset. */
export function Markdown(props: MarkdownProps) {
  return <AstryxMarkdown {...props} components={{ math, ...props.components }} />;
}
