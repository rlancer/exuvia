// Temporary: what is under review, and the choices for each. The first choice
// of each is the site as it ships today and needs no attribute; any other is
// switched on by an attribute on <html> (data-design, data-art).
//
// A design is one stylesheet in src/styles/designs/. An art is one take on the
// hero's lobster: all three are in the page (src/pages/index.astro) and
// designs/shared.css shows the chosen one.
export const CHOICES = [
  {
    key: "design",
    label: "Design",
    options: [
      { id: "current", label: "Current", note: "The print-shop design the site ships with today" },
      { id: "daylight", label: "Daylight", note: "Light and calm: white, harbour navy, one blue" },
      { id: "workbench", label: "Workbench", note: "Dark, in the app's own colours and type" },
      { id: "poster", label: "Poster", note: "Loud: a cobalt field and very large condensed type" },
    ],
  },
  {
    key: "art",
    label: "Art",
    options: [
      { id: "original", label: "Woodcut", note: "The single woodcut lobster the site ships with today" },
      { id: "shells", label: "Shed shells", note: "The woodcut animal and three earlier shells, animated as molts" },
      { id: "poly", label: "Low-poly", note: "A low-poly lobster, morphing up from four older shelled animals" },
    ],
  },
] as const;

/** For the script in Base.astro's head: each key with the ids that set an attribute. */
export const SWITCHES = CHOICES.map((c) => ({ key: c.key, ids: c.options.slice(1).map((o) => o.id as string) }));
