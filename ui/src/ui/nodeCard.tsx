import * as stylex from "@stylexjs/stylex";
import { colorVars, radiusVars, spacingVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Text } from "@astryxdesign/core/Text";
import type { GraphNode } from "../api";
import { countStates, STATE_ORDER } from "../canvasGraph";
import { StateDot } from "./state";

/*
 * The parts a node's card is drawn with, on the canvas (Canvas.tsx) and in the
 * notebook (Notebook.tsx): its state down its leading edge, the ring when
 * something just touched it, the bar while it runs, and a stage's counts.
 */

const REDUCED = "@media (prefers-reduced-motion: reduce)";

/** A node the agent or a run just touched: a ring that spreads from it and fades. */
const flashRing = stylex.keyframes({
  "0%": { boxShadow: `0 0 0 0 color-mix(in srgb, ${colorVars["--color-accent"]} 70%, transparent)` },
  "100%": { boxShadow: "0 0 0 14px transparent" },
});

/** A running node's bar: a light that crosses it. */
const slide = stylex.keyframes({
  from: { backgroundPosition: "100% 0" },
  to: { backgroundPosition: "-100% 0" },
});

export const nodeCard = stylex.create({
  /** The state's colour down the card's leading edge (with a colour from `stripeColor`), in a card whose corners are `--radius-container`. */
  stripe: {
    position: "absolute",
    insetBlock: 0,
    insetInlineStart: 0,
    width: "3px",
    borderStartStartRadius: radiusVars["--radius-container"],
    borderEndStartRadius: radiusVars["--radius-container"],
  },
  /** On the card (keyed by the time it was touched, so it plays again). */
  flash: {
    animationName: { default: flashRing, [REDUCED]: "none" },
    animationDuration: "1.6s",
    animationTimingFunction: "ease-out",
  },
  /** Under the card's head while it runs. */
  busyBar: {
    height: "2px",
    backgroundImage: `linear-gradient(90deg, transparent, ${colorVars["--color-accent"]}, transparent)`,
    backgroundSize: "200% 100%",
    animationName: { default: slide, [REDUCED]: "none" },
    animationDuration: "1.2s",
    animationTimingFunction: "linear",
    animationIterationCount: "infinite",
  },
});

/** The stripe's colour per status (ui/state.tsx): a node that never ran has the rule's grey. */
export const stripeColor = stylex.create({
  success: { backgroundColor: colorVars["--color-success"] },
  warning: { backgroundColor: colorVars["--color-warning"] },
  error: { backgroundColor: colorVars["--color-error"] },
  accent: { backgroundColor: colorVars["--color-accent"] },
  neutral: { backgroundColor: colorVars["--color-border-emphasized"] },
});

/** The count of each state among a stage's nodes, worst first: a dot and a number each. */
export function StateCounts({ members }: { members: GraphNode[] }) {
  const counts = countStates(members);
  return (
    <span {...stylex.props(styles.counts)}>
      {STATE_ORDER.filter((s) => counts[s]).map((s) => (
        <span key={s} {...stylex.props(styles.count)} title={`${counts[s]} ${s}`}>
          <StateDot state={s} label={`${counts[s]} ${s}`} />
          <Text type="supporting" color="secondary" hasTabularNumbers>
            {counts[s]}
          </Text>
        </span>
      ))}
    </span>
  );
}

const styles = stylex.create({
  counts: { display: "inline-flex", alignItems: "center", gap: spacingVars["--spacing-2"] },
  count: { display: "inline-flex", alignItems: "center", gap: spacingVars["--spacing-1"] },
});
