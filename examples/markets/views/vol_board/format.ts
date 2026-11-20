/** 0.1834 → "18.3%" */
export function pct(x: number | null | undefined): string {
  return x == null || Number.isNaN(x) ? "—" : `${(x * 100).toFixed(1)}%`;
}
