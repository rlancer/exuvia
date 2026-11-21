/**
 * The social cards: the picture a link to the site unfolds into when it is
 * shared (`og:image`). Every page names one (layouts/Base.astro), and the
 * brand page shows them all.
 *
 * A card is a 1200 x 630 PNG in `public/og/`. Its source is a page of this
 * site, `/card/<name>/`, which exists only under `astro dev`;
 * `node tools/capture.mjs cards <site url>` photographs each one. The build
 * fails when a card has no PNG, so a docs page added to the site is given
 * its card or stops the build, never shared with the wrong picture.
 */
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { getDocs } from "./docs";

/** Where the site lives, as the cards print it. */
export const HOST = "exuvia.lobster.mp";
export const CARD = { width: 1200, height: 630 };

/**
 * The mark beside the name, as a see-through PNG in `public/brand/`: the one
 * piece of the brand that cannot be a plain SVG, because the name is set in
 * a typeface. `ink` is the name's colour; with `flat` the mark is that
 * colour too, for a place with one ink.
 */
export type Lockup = { name: string; what: string; ink: string; flat?: boolean };
export const LOCKUPS: Lockup[] = [
  { name: "exuvia-lockup-on-dark", what: "The mark and the name, for a dark ground", ink: "#e9e7e1" },
  { name: "exuvia-lockup-on-light", what: "The mark and the name, for a light ground", ink: "#111214" },
  { name: "exuvia-lockup-one-colour", what: "The mark and the name in one ink", ink: "#111214", flat: true },
];

export type Card = {
  /** The PNG is `public/og/<name>.png`. */
  name: string;
  /** How it is drawn (pages/card/[...name].astro). */
  look: "sea" | "docs" | "doc" | "brand";
  /** The page it is for, root-relative. */
  page: string;
  /** Where in the site that page is. */
  crumb?: string;
  title: string;
  summary: string;
  /** The animal of src/lib/poly.ts drawn on it. */
  animal: string;
  /** What the picture shows, for someone who cannot see it. */
  alt: string;
};

// The docs run from the oldest animal to the lobster: each group of pages has
// one of the five, in outline, on its cards.
const ANIMALS: Record<string, string> = {
  Start: "Waptia",
  Build: "Ceratiocaris",
  Run: "Palaeopalaemon",
  Integrate: "Eryma",
  Project: "Homarus",
};

export async function getCards(): Promise<Card[]> {
  const docs = await getDocs();
  return [
    {
      name: "home",
      look: "sea",
      page: "/",
      title: "A data workbench where the graph is the interface",
      summary: "Plain Python and SQL files, per-node schedules, an agent on the graph.",
      animal: "Homarus",
      alt: "exuvia: a data workbench where the graph is the interface. A low-poly blue lobster under water, with the outlines of older shelled animals behind it.",
    },
    {
      name: "docs",
      look: "docs",
      page: "/docs/",
      crumb: "Docs",
      title: "Documentation",
      summary: "Guides and reference: nodes, schedules, the agent, views, deployment and the HTTP API.",
      animal: "Homarus",
      alt: "exuvia documentation. A low-poly blue lobster, and above it the outlines of four older shelled animals.",
    },
    ...docs.map(
      (doc): Card => ({
        name: `docs-${doc.slug}`,
        look: "doc",
        page: `/docs/${doc.slug}/`,
        crumb: `Docs / ${doc.group}`,
        title: doc.title,
        summary: doc.summary,
        animal: ANIMALS[doc.group] ?? "Homarus",
        alt: `exuvia docs: ${doc.title}. ${doc.summary}`,
      }),
    ),
    {
      name: "brand",
      look: "brand",
      page: "/brand/",
      crumb: "Brand",
      title: "The exuvia brand",
      summary: "The name, the mark, the colours, the type and the animals.",
      animal: "Homarus",
      alt: "The exuvia brand: the mark, a low-poly lobster's claw, above the colours of the site.",
    },
  ];
}

/** The card called `name`, and the address of its PNG. */
export async function getCard(name: string): Promise<Card & { src: string }> {
  const card = (await getCards()).find((c) => c.name === name);
  if (!card) throw new Error(`No social card called "${name}" (src/lib/cards.ts).`);
  const file = path.join(process.cwd(), "public", "og", `${name}.png`);
  if (!existsSync(file)) {
    // While the cards are being drawn there is nothing to photograph yet.
    if (import.meta.env.DEV) return { ...card, src: `/og/${name}.png` };
    throw new Error(
      `The social card public/og/${name}.png is missing. With the site running (npm run dev): node tools/capture.mjs cards http://localhost:4380`,
    );
  }
  // Sites that unfold links keep the picture by its address, so the address
  // changes when the picture does.
  const stamp = createHash("sha1").update(readFileSync(file)).digest("hex").slice(0, 8);
  return { ...card, src: `/og/${name}.png?v=${stamp}` };
}
