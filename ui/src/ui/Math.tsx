import katex from "katex";
import "katex/dist/katex.min.css";
import { colorVars } from "@astryxdesign/core/theme/tokens.stylex";

/**
 * A `$…$` or `$$…$$` expression typeset by KaTeX. Its own chunk, with KaTeX's
 * stylesheet and fonts: fetched the first time a page shows maths.
 *
 * KaTeX escapes the expression and, with `trust` off (its default), refuses
 * the commands that could add links, classes or HTML, so its markup is safe
 * to set. An expression it can't parse shows as written, in the error colour.
 */
export default function MathExpression({ value, display }: { value: string; display: "inline" | "block" }) {
  const html = katex.renderToString(value, {
    displayMode: display === "block",
    throwOnError: false,
    errorColor: colorVars["--color-text-error"],
    output: "htmlAndMathml",
    // No rule or space wider than a line: `\rule{200em}{1em}` must not stretch the page.
    maxSize: 40,
  });
  return display === "block" ? <div dangerouslySetInnerHTML={{ __html: html }} /> : <span dangerouslySetInnerHTML={{ __html: html }} />;
}
