/**
 * What the project's bar shows at a width: the richest of its layouts that
 * fits, giving up words before controls. Pure, so it is tested on its own
 * (barLayout.test.ts). The bar is measured, not the window: the side panel
 * beside it takes 340px of a wide window.
 *
 *   1. everything: Canvas / Notebook with their words, the state counts,
 *      the graph's buttons with their words
 *   2. Canvas / Notebook as icons
 *   3. the buttons as icons too
 *   4. without the state counts (the canvas's nodes carry them too)
 *
 * The tabs keep their words throughout. On a phone (App.tsx) they lose their
 * counts and Canvas / Notebook is one button instead.
 */
export interface BarLayout {
  toggleLabels: boolean;
  buttonLabels: boolean;
  counts: boolean;
}

/** Widths measured in the app (px). */
export const BAR = {
  padding: 32,
  gap: 12,
  tabs: 152,
  toggleLabels: 173,
  toggleIcons: 66,
  /** One state's dot, number and word ("9 fresh"). */
  perState: 62,
  buttonsLabels: 252,
  buttonsIcons: 80,
};

const LAYOUTS: BarLayout[] = [
  { toggleLabels: true, buttonLabels: true, counts: true },
  { toggleLabels: false, buttonLabels: true, counts: true },
  { toggleLabels: false, buttonLabels: false, counts: true },
  { toggleLabels: false, buttonLabels: false, counts: false },
];

/** What a layout needs, for a bar with `states` states to count, the graph's buttons (an editor's) and the layout switch (a graph with nodes). */
export function barWidth(l: BarLayout, has: { states: number; buttons: boolean; toggle: boolean }): number {
  let w = BAR.padding + BAR.tabs + BAR.gap; // the space that pushes the rest to the end
  if (has.toggle) w += (l.toggleLabels ? BAR.toggleLabels : BAR.toggleIcons) + BAR.gap;
  if (l.counts && has.states) w += has.states * BAR.perState + BAR.gap;
  if (has.buttons) w += (l.buttonLabels ? BAR.buttonsLabels : BAR.buttonsIcons) + BAR.gap;
  return w;
}

export function barLayout(width: number, has: { states: number; buttons: boolean; toggle: boolean }): BarLayout {
  return LAYOUTS.find((l) => barWidth(l, has) <= width) ?? LAYOUTS[LAYOUTS.length - 1];
}
