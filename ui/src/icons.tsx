import type { ReactNode } from "react";

/**
 * The app's icons: one 16 x 16 grid and one stroke, so they sit alike next to
 * text on every platform (the glyphs they replace, such as a stopwatch or a
 * moon, are drawn as colour emoji by some systems and as text by others).
 */
function icon(body: ReactNode, filled = false) {
  return function Icon({ size = 14 }: { size?: number }) {
    return (
      <svg
        className="icon"
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
export const IconClock = icon(<path d="M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2zM8 4.8V8l2.1 1.4" />);
export const IconSun = icon(<path d="M8 5.2a2.8 2.8 0 1 0 0 5.6 2.8 2.8 0 0 0 0-5.6zM8 1.5v1.3M8 13.2v1.3M1.5 8h1.3M13.2 8h1.3M3.4 3.4l.9.9M11.7 11.7l.9.9M3.4 12.6l.9-.9M11.7 4.3l.9-.9" />);
export const IconMoon = icon(<path d="M13.5 9.6A5.8 5.8 0 0 1 6.4 2.5a5.8 5.8 0 1 0 7.1 7.1z" />);
export const IconChevronRight = icon(<path d="M6 3.5L10.5 8 6 12.5" />);
export const IconChevronLeft = icon(<path d="M10 3.5L5.5 8l4.5 4.5" />);
export const IconPackage = icon(<path d="M2.5 5L8 2l5.5 3v6L8 14l-5.5-3zM2.5 5L8 8l5.5-3M8 8v6" />);
export const IconAlert = icon(<path d="M8 2.2l6.2 10.9H1.8zM8 6.5v3M8 11.4v.1" />);
/** The agent's sign: a four-pointed spark. */
export const IconSpark = icon(<path d="M8 1.5l1.5 4.2a1.3 1.3 0 0 0 .8.8L14.5 8l-4.2 1.5a1.3 1.3 0 0 0-.8.8L8 14.5l-1.5-4.2a1.3 1.3 0 0 0-.8-.8L1.5 8l4.2-1.5a1.3 1.3 0 0 0 .8-.8z" />, true);

/** "Ctrl" on most keyboards, "⌘" on a Mac: for the hints next to shortcuts. */
export const MOD = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl+";
