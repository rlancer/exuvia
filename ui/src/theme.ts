import { useEffect, useState } from "react";

/** Light or dark: the system's, until the toggle picks one (kept in localStorage). */
export function useTheme(): [boolean, () => void] {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const [pref, setPref] = useState<string | null>(() => {
    try {
      return localStorage.getItem("exuvia-theme");
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
  useEffect(() => {
    if (pref) document.documentElement.dataset.theme = pref;
    else delete document.documentElement.dataset.theme;
  }, [pref]);
  const toggle = () => {
    const next = dark ? "light" : "dark";
    setPref(next);
    try {
      localStorage.setItem("exuvia-theme", next);
    } catch {
      /* private mode */
    }
  };
  return [dark, toggle];
}
