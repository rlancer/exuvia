// Temporary: the design variations under review. "current" is the site as it
// ships today; each of the others is one stylesheet in src/styles/designs/,
// switched on by `data-design` on <html>.
export const DESIGNS = [
  { id: "current", label: "Current", note: "The print-shop design the site ships with today" },
  { id: "daylight", label: "Daylight", note: "Light and calm: white, harbour navy, one blue" },
  { id: "workbench", label: "Workbench", note: "Dark, in the app's own colours and type" },
  { id: "poster", label: "Poster", note: "Loud: a cobalt field and very large condensed type" },
] as const;

export const DESIGN_IDS = DESIGNS.map((d) => d.id).filter((id) => id !== "current");
