import { lazy, Suspense } from "react";
import * as stylex from "@stylexjs/stylex";
import { Link } from "@astryxdesign/core/Link";
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

/** What a Markdown image shows as: itself when it is `inline`, a link to open when it is `link`, else its alt text. */
export function imageAs(src: string): "inline" | "link" | "text" {
  if (/^data:image\/(png|jpe?g|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(src)) return "inline";
  return /^https?:\/\//i.test(src) ? "link" : "text";
}

/**
 * An image in Markdown is not fetched. The text is someone else's (a node's
 * docstring in a shared project, an agent's reply), and an `<img>` makes
 * every reader's browser ask for its address, signed in when it is this
 * server's: a way to follow who reads, and how a reply written under a
 * prompt injection sends out what it was shown. An image that carries its
 * own bytes (`data:image/png;base64,…`) is drawn; one at an address is a
 * link to open.
 */
const image: NonNullable<MarkdownComponents["image"]> = ({ src, alt }) => {
  const as = imageAs(src);
  if (as === "inline") return <img src={src} alt={alt} {...stylex.props(styles.image)} />;
  if (as === "link")
    return (
      <Link href={src} isExternalLink>
        {alt || src}
      </Link>
    );
  return <>{alt}</>;
};

const styles = stylex.create({
  image: { maxWidth: "100%" },
});

/** Markdown as exuvia shows it everywhere: Astryx's, with maths typeset and images not fetched. */
export function Markdown(props: MarkdownProps) {
  return <AstryxMarkdown {...props} components={{ math, image, ...props.components }} />;
}
