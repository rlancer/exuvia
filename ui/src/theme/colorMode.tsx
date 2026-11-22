import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

interface ColorMode {
  /** Whether the page is drawn dark now. */
  dark: boolean;
  /** What the toggle picked, or null to follow the system. */
  pref: "light" | "dark" | null;
  toggle: () => void;
}

const ColorModeContext = createContext<ColorMode>({ dark: false, pref: null, toggle: () => {} });

/** Light or dark: the system's, until the toggle picks one (kept in localStorage). */
export function ColorModeProvider({ children }: { children: ReactNode }) {
  const media = useMemo(() => window.matchMedia("(prefers-color-scheme: dark)"), []);
  const [pref, setPref] = useState<"light" | "dark" | null>(() => {
    try {
      const kept = localStorage.getItem("exuvia-theme");
      return kept === "light" || kept === "dark" ? kept : null;
    } catch {
      return null;
    }
  });
  const [sysDark, setSysDark] = useState(media.matches);
  useEffect(() => {
    const on = (e: MediaQueryListEvent) => setSysDark(e.matches);
    media.addEventListener("change", on);
    return () => media.removeEventListener("change", on);
  }, [media]);
  const dark = pref ? pref === "dark" : sysDark;
  const value = useMemo<ColorMode>(
    () => ({
      dark,
      pref,
      toggle: () => {
        const next = dark ? "light" : "dark";
        setPref(next);
        try {
          localStorage.setItem("exuvia-theme", next);
        } catch {
          /* private mode */
        }
      },
    }),
    [dark, pref],
  );
  return <ColorModeContext.Provider value={value}>{children}</ColorModeContext.Provider>;
}

/** `[dark, toggle]`: whether the page is dark, and the top bar's switch. */
export function useColorMode(): [boolean, () => void] {
  const { dark, toggle } = useContext(ColorModeContext);
  return [dark, toggle];
}

/** What Astryx's `<Theme mode>` takes: the pick, or the system's. */
export function useThemeMode(): "light" | "dark" | "system" {
  return useContext(ColorModeContext).pref ?? "system";
}
