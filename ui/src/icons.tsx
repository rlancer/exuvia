import type { ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";

const styles = stylex.create({ icon: { display: "block", flexShrink: 0 } });

/**
 * The app's icons: one 16 x 16 grid and one stroke, so they sit alike next to
 * text on every platform (the glyphs they replace, such as a stopwatch or a
 * moon, are drawn as colour emoji by some systems and as text by others).
 */
function icon(body: ReactNode, filled = false) {
  return function Icon({ size = 14 }: { size?: number }) {
    return (
      <svg
        {...stylex.props(styles.icon)}
        width={size}
        height={size}
        viewBox="0 0 16 16"
        fill={filled ? "currentColor" : "none"}
        stroke={filled ? "none" : "currentColor"}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
        focusable="false"
      >
        {body}
      </svg>
    );
  };
}

export const IconPlay = icon(<path d="M4.5 2.8v10.4a.6.6 0 0 0 .92.5l8.1-5.2a.6.6 0 0 0 0-1L5.42 2.3a.6.6 0 0 0-.92.5z" />, true);
export const IconPlus = icon(<path d="M8 3v10M3 8h10" />);
export const IconClose = icon(<path d="M3.5 3.5l9 9M12.5 3.5l-9 9" />);
export const IconTrash = icon(<path d="M2.5 4.5h11M6.5 4.5V3a.5.5 0 0 1 .5-.5h2a.5.5 0 0 1 .5.5v1.5M4 4.5l.6 8.1a1 1 0 0 0 1 .9h4.8a1 1 0 0 0 1-.9l.6-8.1M6.6 7v4M9.4 7v4" />);
export const IconTag = icon(<path d="M2.5 3.3v3.9a1 1 0 0 0 .3.7l5.6 5.6a1 1 0 0 0 1.4 0l3.4-3.4a1 1 0 0 0 0-1.4L7.6 3.1a1 1 0 0 0-.7-.3H3a.5.5 0 0 0-.5.5zM5.4 5.4h.01" />);
export const IconClock = icon(<path d="M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2zM8 4.8V8l2.1 1.4" />);
export const IconSun = icon(<path d="M8 5.2a2.8 2.8 0 1 0 0 5.6 2.8 2.8 0 0 0 0-5.6zM8 1.5v1.3M8 13.2v1.3M1.5 8h1.3M13.2 8h1.3M3.4 3.4l.9.9M11.7 11.7l.9.9M3.4 12.6l.9-.9M11.7 4.3l.9-.9" />);
export const IconMoon = icon(<path d="M13.5 9.6A5.8 5.8 0 0 1 6.4 2.5a5.8 5.8 0 1 0 7.1 7.1z" />);
export const IconChevronRight = icon(<path d="M6 3.5L10.5 8 6 12.5" />);
export const IconChevronLeft = icon(<path d="M10 3.5L5.5 8l4.5 4.5" />);
export const IconKey = icon(<path d="M10.5 2.5a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM8.4 7.6 2.5 13.5M4.5 11.5l1.5 1.5M6 10l1.3 1.3" />);
export const IconPackage = icon(<path d="M2.5 5L8 2l5.5 3v6L8 14l-5.5-3zM2.5 5L8 8l5.5-3M8 8v6" />);
export const IconCode = icon(<path d="m5 4-3.5 4L5 12m6-8 3.5 4-3.5 4M9.5 3l-3 10" />);
export const IconHelp = icon(<path d="M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2zM6.3 6.2a1.8 1.8 0 0 1 3.5.5c0 1.2-1.8 1.5-1.8 2.6M8 11.3v.1" />);
export const IconAlert = icon(<path d="M8 2.2l6.2 10.9H1.8zM8 6.5v3M8 11.4v.1" />);
/** A plug: outside tools plugged into the agent (its MCP servers). */
export const IconPlug = icon(<path d="M6 1.8v3M10 1.8v3M4.5 4.8h7v2.7a3.5 3.5 0 0 1-7 0zM8 11v3.2" />);
/** The agent's sign: a four-pointed spark. */
export const IconSpark = icon(<path d="M8 1.5l1.5 4.2a1.3 1.3 0 0 0 .8.8L14.5 8l-4.2 1.5a1.3 1.3 0 0 0-.8.8L8 14.5l-1.5-4.2a1.3 1.3 0 0 0-.8-.8L1.5 8l4.2-1.5a1.3 1.3 0 0 0 .8-.8z" />, true);

/** "Ctrl" on most keyboards, "⌘" on a Mac: for the hints next to shortcuts. */
export const MOD = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl+";
export const IconUser = icon(
  <>
    <circle cx="8" cy="5.5" r="2.6" />
    <path d="M2.8 13.6c.7-2.5 2.7-3.9 5.2-3.9s4.5 1.4 5.2 3.9" />
  </>,
);
/** Home: the hub's projects page. */
export const IconHome = icon(<path d="M2.5 7.2 8 2.5l5.5 4.7M4 6v7a.5.5 0 0 0 .5.5h2.5V10h2v3.5h2.5a.5.5 0 0 0 .5-.5V6" />);
export const IconFolder = icon(<path d="M1.8 4.2a1 1 0 0 1 1-1h3.1l1.5 1.6h5.8a1 1 0 0 1 1 1v6.7a1 1 0 0 1-1 1H2.8a1 1 0 0 1-1-1z" />);
export const IconFile = icon(<path d="M4 1.8h5.2L12.5 5v8.2a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V2.8a1 1 0 0 1 1-1zM9 1.8V5.3h3.5" />);
export const IconDownload = icon(<path d="M8 2v8.5M4.5 7 8 10.5 11.5 7M2.5 11.5v1.5a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1.5" />);
/** Share: three linked nodes. */
export const IconShare = icon(
  <>
    <circle cx="12" cy="3.6" r="1.9" />
    <circle cx="4" cy="8" r="1.9" />
    <circle cx="12" cy="12.4" r="1.9" />
    <path d="M5.7 7.1l4.6-2.6M5.7 8.9l4.6 2.6" />
  </>,
);
/** A view as an app: arrows out to two corners. */
export const IconExpand = icon(<path d="M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9" />);
/** The app mode's tools: three sliders. */
export const IconSliders = icon(<path d="M2.5 4h6M11.5 4h2M2.5 8h2M7.5 8h6M2.5 12h7M12.5 12h1M10 2.5v3M6 6.5v3M11 10.5v3" />);
/** Copy: two sheets. */
export const IconCopy = icon(<path d="M5.5 5.5V3a.5.5 0 0 1 .5-.5h7a.5.5 0 0 1 .5.5v7a.5.5 0 0 1-.5.5h-2.5M3 5.5h7a.5.5 0 0 1 .5.5v7a.5.5 0 0 1-.5.5H3a.5.5 0 0 1-.5-.5V6a.5.5 0 0 1 .5-.5z" />);
/** Stages: layers stacked, for stages of nodes. */
export const IconLayers = icon(<path d="M8 2.2 1.8 5.4 8 8.6l6.2-3.2L8 2.2zM1.8 8.2 8 11.4l6.2-3.2M1.8 10.9 8 14.1l6.2-3.2" />);
/** A view: a window with its bar. */
export const IconWindow = icon(<path d="M2.5 3.2h11a.7.7 0 0 1 .7.7v8.2a.7.7 0 0 1-.7.7h-11a.7.7 0 0 1-.7-.7V3.9a.7.7 0 0 1 .7-.7zM1.8 6h12.4" />);
/** What the graph is doing: a pulse line. */
export const IconActivity = icon(<path d="M1.5 8.5h3l1.8-4.5 3.4 8 1.8-3.5h3" />);
/** Fold a stage into one card, or open it out. */
export const IconCollapse = icon(<path d="M4.5 2.5 8 6l3.5-3.5M4.5 13.5 8 10l3.5 3.5" />);
export const IconUncollapse = icon(<path d="M4.5 5.5 8 2l3.5 3.5M4.5 10.5 8 14l3.5-3.5" />);
/** The graph drawn as a canvas: nodes and the edges between them. */
export const IconGraph = icon(<path d="M4 2.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zM12 2.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zM8 10.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zM4.8 5.3l2.4 5.4M11.2 5.3l-2.4 5.4" />);
/** The graph read as a notebook: a page of cells, top to bottom. */
export const IconNotebook = icon(<path d="M3.5 1.8h9a.7.7 0 0 1 .7.7v11a.7.7 0 0 1-.7.7h-9a.7.7 0 0 1-.7-.7v-11a.7.7 0 0 1 .7-.7zM5.5 4.8h5M5.5 8h5M5.5 11.2h3" />);
/** A table of contents: entries, the second level indented. */
export const IconContents = icon(<path d="M2.5 3.5h.01M5 3.5h8.5M5 8h.01M7.5 8h6M5 12.5h.01M7.5 12.5h6" />);
