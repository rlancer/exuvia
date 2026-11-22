import * as stylex from "@stylexjs/stylex";
import { colorVars } from "@astryxdesign/core/theme/tokens.stylex";
import { StatusDot } from "@astryxdesign/core/StatusDot";

/** Astryx's statuses, which the states of a node, a run and a view are drawn in. */
export type Status = "success" | "warning" | "error" | "accent" | "neutral";

/**
 * The one mapping from a state to its colour: fresh and success are green,
 * stale is amber, failed and error are red, running and queued are the
 * accent, and everything else (new, cached, skipped) is neutral.
 */
export function statusOf(state: string | null | undefined): Status {
  switch (state) {
    case "fresh":
    case "success":
    case "ready":
      return "success";
    case "stale":
      return "warning";
    case "failed":
    case "error":
      return "error";
    case "running":
    case "queued":
    case "installing":
    case "pending":
      return "accent";
    default:
      return "neutral";
  }
}

/** A state's dot. It pulses while the thing is running. */
export function StateDot({ state, label }: { state: string | null | undefined; label?: string }) {
  return <StatusDot variant={statusOf(state)} label={label ?? state ?? "unknown"} isPulsing={state === "running"} />;
}

/** Text in a state's colour: `<Text xstyle={statusText[statusOf(state)]}>`. */
export const statusText = stylex.create({
  success: { color: colorVars["--color-text-success"] },
  warning: { color: colorVars["--color-text-warning"] },
  error: { color: colorVars["--color-text-error"] },
  accent: { color: colorVars["--color-text-accent"] },
  neutral: { color: colorVars["--color-text-secondary"] },
});
