/** Hand-highlighting for the annotated listings (components/Anatomy.astro). */
import { escapeHtml } from "./highlight";

export const k = (t: string) => `<span class="k">${t}</span>`;
export const s = (t: string) => `<span class="s">${t}</span>`;
export const f = (t: string) => `<span class="f">${t}</span>`;
export const c = (t: string) => `<span class="c">${t}</span>`;
/** A span keyed to the note with the same letter. */
export const ref = (id: string, t: string) => `<mark data-ref="${id}">${t}</mark>`;
/** Source text that contains `<`, `>` or `&`. */
export const x = escapeHtml;
