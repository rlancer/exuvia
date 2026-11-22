import type { ReactNode } from "react";
import * as stylex from "@stylexjs/stylex";
import { colorVars, typeScaleVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { Theme } from "@astryxdesign/core/theme";
// The theme as `bun run theme` built it from theme/exuviaTheme.ts; its CSS is imported in main.tsx.
import { exuviaTheme } from "./theme/exuvia";
import { ColorModeProvider, useThemeMode } from "./theme/colorMode";

const styles = stylex.create({
  page: {
    minHeight: "100dvh",
    backgroundColor: colorVars["--color-background-body"],
    color: colorVars["--color-text-primary"],
    fontFamily: typographyVars["--font-family-body"],
    fontSize: typeScaleVars["--text-body-size"],
    lineHeight: typeScaleVars["--text-body-leading"],
    WebkitFontSmoothing: "antialiased",
  },
});

/** What every page of the app stands in: the colour mode, the exuvia theme and the page's ground. */
export function Root({ children }: { children: ReactNode }) {
  return (
    <ColorModeProvider>
      <Themed>{children}</Themed>
    </ColorModeProvider>
  );
}

function Themed({ children }: { children: ReactNode }) {
  return (
    <Theme theme={exuviaTheme} mode={useThemeMode()}>
      <div {...stylex.props(styles.page)}>{children}</div>
    </Theme>
  );
}
