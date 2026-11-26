/**
 * Which rows of a long table are drawn, and which pages of it are held: the
 * arithmetic of the Data tab's table (NodeOutput's RowsTable and useRowPages),
 * pure so it is tested on its own (rowWindow.test.ts).
 */

/** Rows a page of an output holds: one request to GET /nodes/{name}/data. */
export const PAGE_ROWS = 500;

/**
 * The tallest the rows' area is made, in px. A browser stops laying an
 * element out past a height of its own (Firefox near 17.8 million px, Chrome
 * near 33.5 million), which a million rows already reach.
 */
export const MAX_ROWS_HEIGHT = 10_000_000;

export interface RowWindow {
  /** The rows drawn: `start` up to, not including, `end`. */
  start: number;
  end: number;
  /** The empty space over and under them that stands for the rows not drawn. */
  padTop: number;
  padBottom: number;
}

/**
 * The rows to draw of `total`, each `rowHeight` tall, when the rows' area is
 * scrolled to `scrollTop` and `viewport` of it shows: the ones in view and
 * `overscan` more on each side, so a scroll shows rows at once.
 *
 * While every row fits under `maxHeight`, a row is where its number puts it.
 * Past that the area is `maxHeight` tall and the scroll position is a share
 * of the way through the rows: the first row in view is that share of them,
 * drawn where the area is scrolled to.
 */
export function rowWindow(o: {
  scrollTop: number;
  viewport: number;
  rowHeight: number;
  total: number;
  overscan?: number;
  maxHeight?: number;
}): RowWindow {
  const { total, rowHeight } = o;
  const overscan = o.overscan ?? 10;
  const maxHeight = o.maxHeight ?? MAX_ROWS_HEIGHT;
  if (total <= 0 || rowHeight <= 0) return { start: 0, end: 0, padTop: 0, padBottom: 0 };
  const inView = Math.max(1, Math.ceil(o.viewport / rowHeight));
  const full = total * rowHeight;
  const scrollTop = Math.max(0, o.scrollTop);
  if (full <= maxHeight) {
    const first = Math.min(Math.floor(scrollTop / rowHeight), Math.max(0, total - 1));
    const start = Math.max(0, first - overscan);
    const end = Math.min(total, first + inView + overscan);
    return { start, end, padTop: start * rowHeight, padBottom: (total - end) * rowHeight };
  }
  const furthest = Math.max(1, maxHeight - o.viewport);
  const share = Math.min(1, scrollTop / furthest);
  const first = Math.round(share * Math.max(0, total - inView));
  const start = Math.max(0, first - overscan);
  const end = Math.min(total, first + inView + overscan);
  const drawn = (end - start) * rowHeight;
  const padTop = Math.max(0, Math.min(scrollTop - (first - start) * rowHeight, maxHeight - drawn));
  return { start, end, padTop, padBottom: Math.max(0, maxHeight - padTop - drawn) };
}

/** The pages (of `size` rows) that hold rows `start` up to `end`, in order. */
export function pagesFor(start: number, end: number, size = PAGE_ROWS): number[] {
  if (end <= start) return [];
  const out: number[] = [];
  for (let p = Math.floor(start / size); p <= Math.floor((end - 1) / size); p++) out.push(p);
  return out;
}

/**
 * The pages to let go of so that no more than `keep` are held: the ones
 * furthest from the pages in view (`from` to `to`).
 */
export function pagesToDrop(held: number[], from: number, to: number, keep: number): number[] {
  if (held.length <= keep) return [];
  const away = (p: number) => (p < from ? from - p : p > to ? p - to : 0);
  return [...held].sort((a, b) => away(b) - away(a)).slice(0, held.length - keep);
}

/**
 * How many characters wide each column's cells are, of `rows` as `text`
 * writes a cell, no narrower than `floor` (what it was before) and no wider
 * than `cap`: what a column is kept at least as wide as, so it does not
 * change width as rows scroll through it.
 */
export function columnChars(rows: unknown[][], text: (v: unknown) => string, floor: number[], cap: number): number[] {
  const out = [...floor];
  for (const row of rows) {
    for (let c = 0; c < row.length; c++) {
      if ((out[c] ?? 0) >= cap) continue;
      const n = Math.min(cap, text(row[c]).length);
      if (n > (out[c] ?? 0)) out[c] = n;
    }
  }
  return out;
}
