import { lazy } from "react";

/**
 * Astryx's Markdown renderer, fetched the first time something shows Markdown
 * (an agent's reply, a node's docstring in the notebook): a page with none
 * costs nothing. Render it inside a Suspense whose fallback is the plain text.
 */
export const Markdown = lazy(() => import("@astryxdesign/core/Markdown").then((m) => ({ default: m.Markdown })));
