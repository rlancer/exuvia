/**
 * Turns a generated woodcut (black ink on white paper) into a transparent
 * PNG of pure ink in one of the site's inks, so the page can print it on
 * paper. The colours here match --ink and --blue in src/styles/global.css.
 *
 *   node tools/ink.mjs lobster <generated.png>   -> src/assets/lobster.png
 *   node tools/ink.mjs growth  <generated.png>   -> src/assets/growth-{1,2,3}.png (one per animal)
 *
 * The sources came from OpenRouter (google/gemini-3-pro-image) through
 * marketing/trailer/tools/or-media.mjs, with the prompts in ART below.
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const STYLE =
  "A rustic hand-cut wood engraving in the manner of Thomas Bewick, printed in pure black ink on plain white " +
  "paper. Bold confident black line work, dense cross-hatching for shadow, slightly uneven hand-printed ink, " +
  "strong black-and-white contrast with no greys. Pure white background (#ffffff). No text, no labels, no " +
  "numbers, no border, no frame, no colour.";

const BLACK = [17, 17, 16];
const BLUE = [109, 156, 254];

export const ART = {
  lobster: {
    inks: [BLUE],
    aspect: "4:3",
    prompt:
      `${STYLE} Subject: a lobster exuvia, the complete, empty, molted shell of a lobster, claws, antennae and ` +
      "every leg segment intact but hollow. Viewed from above, the whole specimen centred with generous empty " +
      "white margin on all sides, nothing cropped.",
    // The model drew the print photographed on a sheet; keep the block, drop the sheet's edges.
    crop: { left: 0.125, top: 0.195, width: 0.755, height: 0.61 },
  },
  growth: {
    // The first two generations in black, the one that is exuvia in blue.
    inks: [BLACK, BLACK, BLUE],
    aspect: "21:9",
    prompt:
      `${STYLE} Subject: three lobsters in a single horizontal row, seen from above, all facing right, evenly ` +
      "spaced, growing from left to right: small, medium, large, like three stages of growth of the same " +
      "animal. Generous empty white margin around and between the three, nothing cropped, nothing overlapping.",
  },
};

const [name, source] = process.argv.slice(2);
if (!ART[name] || !source) {
  console.error("usage: node tools/ink.mjs <lobster|growth> <generated.png>");
  process.exit(2);
}

const out = path.resolve(path.dirname(fileURLToPath(import.meta.url)), `../src/assets/${name}.png`);
let image = sharp(source).removeAlpha();
const crop = ART[name].crop;
if (crop) {
  const meta = await sharp(source).metadata();
  image = image.extract({
    left: Math.round(crop.left * meta.width),
    top: Math.round(crop.top * meta.height),
    width: Math.round(crop.width * meta.width),
    height: Math.round(crop.height * meta.height),
  });
}
const { data, info } = await image.greyscale().raw().toBuffer({ resolveWithObject: true });

// Luminance becomes ink coverage: paper (light) is clear, ink (dark) is solid.
const [PAPER, INK] = [170, 60];
const rgba = Buffer.alloc(info.width * info.height * 4);
for (let i = 0, o = 0; i < data.length; i++, o += 4) {
  const ink = Math.min(1, Math.max(0, (PAPER - data[i]) / (PAPER - INK)));
  rgba[o + 3] = Math.round(ink * 255);
}

/** Paints a raw RGBA buffer (alpha already set) in one ink. */
function print(buffer, width, height, [r, g, b]) {
  for (let o = 0; o < buffer.length; o += 4) {
    buffer[o] = r;
    buffer[o + 1] = g;
    buffer[o + 2] = b;
  }
  return sharp(buffer, { raw: { width, height, channels: 4 } });
}
const save = (img, file) => img.png({ compressionLevel: 9, palette: true, colours: 16 }).toFile(file);

if (name !== "growth") {
  await save(print(rgba, info.width, info.height, ART[name].inks[0]).trim(), out);
  console.log("wrote", out);
} else {
  // Split the row into its animals: runs of inked columns between blank gutters.
  const filled = Array.from({ length: info.width }, (_, x) => {
    let sum = 0;
    for (let y = 0; y < info.height; y++) sum += rgba[(y * info.width + x) * 4 + 3];
    return sum > 255 * 2;
  });
  const runs = [];
  for (let x = 0, start = -1; x <= info.width; x++) {
    if (filled[x] && start < 0) start = x;
    if (!filled[x] && start >= 0) {
      const last = runs.at(-1);
      if (last && start - last[1] < 24) last[1] = x; // a hairline gap inside one animal
      else runs.push([start, x]);
      start = -1;
    }
  }
  const animals = runs.filter(([a, b]) => b - a > 60);
  if (animals.length !== 3) throw new Error("expected 3 animals, found " + animals.length);
  for (const [i, [a, b]] of animals.entries()) {
    const file = out.replace(/.png$/, "-" + (i + 1) + ".png");
    // Copy the columns out by hand: sharp trims before it extracts.
    const width = b - a;
    const part = Buffer.alloc(width * info.height * 4);
    for (let y = 0; y < info.height; y++) {
      rgba.copy(part, y * width * 4, (y * info.width + a) * 4, (y * info.width + b) * 4);
    }
    await save(print(part, width, info.height, ART[name].inks[i]).trim(), file);
    console.log("wrote", file);
  }
}
