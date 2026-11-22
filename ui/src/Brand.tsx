import type { ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { durationVars, fontWeightVars, radiusVars, spacingVars, textSizeVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Text } from "@astryxdesign/core/Text";
import { MARK } from "./mark";

const styles = stylex.create({
  mark: { display: "block", flexShrink: 0 },
  // The claw gives a nip when the name is pointed at, as it does on the website.
  nip: {
    transformOrigin: "25% 80%",
    transitionProperty: "transform",
    transitionDuration: durationVars["--duration-fast"],
    transform: { default: "none", [stylex.when.ancestor(":hover")]: "rotate(-12deg)" },
  },
  brand: { display: "flex", alignItems: "center", gap: spacingVars["--spacing-2"], minWidth: 0 },
  name: {
    display: "inline-flex",
    alignItems: "center",
    gap: spacingVars["--spacing-2"],
    flexShrink: 0,
    color: "inherit",
    textDecoration: "none",
    borderRadius: radiusVars["--radius-inner"],
  },
  wordmark: {
    fontFamily: typographyVars["--font-family-heading"],
    fontWeight: fontWeightVars["--font-weight-bold"],
    fontSize: textSizeVars["--font-size-lg"],
    lineHeight: 1,
    letterSpacing: "-0.04em",
  },
  where: { display: "inline-flex", alignItems: "center", gap: spacingVars["--spacing-2"], minWidth: 0 },
  // The name is still in the tab's title where there is no room for it.
  wide: { display: { default: "inline-flex", "@media (max-width: 560px)": "none" } },
});

/** The exuvia mark. With `flat` it is one shape in the colour of the text around it. */
export function Mark({ size = 20, flat = false, nips = false }: { size?: number; flat?: boolean; nips?: boolean }) {
  return (
    <svg {...stylex.props(styles.mark, nips && styles.nip)} width={size} height={size} viewBox={`0 0 ${MARK.box} ${MARK.box}`} aria-hidden focusable="false">
      {flat ? (
        <path d={MARK.outline} fill="currentColor" />
      ) : (
        <g strokeWidth="0.6" strokeLinejoin="round">
          {MARK.facets.map(([d, tone]) => (
            <path key={d} d={d} fill={tone} stroke={tone} />
          ))}
        </g>
      )}
    </svg>
  );
}

/**
 * The top bar's lockup: the mark, the name, and where you are. `home` makes
 * the name a link back to the hub's list of projects.
 */
export function Brand({ home, children }: { home?: string; children?: ReactNode }) {
  const name = (
    <>
      <Mark nips={!!home} />
      <span {...stylex.props(styles.wordmark, styles.wide)}>exuvia</span>
    </>
  );
  return (
    <div {...stylex.props(styles.brand)}>
      {home ? (
        <a {...stylex.props(styles.name, stylex.defaultMarker())} href={home} title="All projects" aria-label="exuvia: all projects">
          {name}
        </a>
      ) : (
        <span {...stylex.props(styles.name)}>{name}</span>
      )}
      {children != null && (
        <span {...stylex.props(styles.where, styles.wide)}>
          <Text color="disabled" aria-hidden>
            /
          </Text>
          <Text weight="medium" maxLines={1}>
            {children}
          </Text>
        </span>
      )}
    </div>
  );
}
