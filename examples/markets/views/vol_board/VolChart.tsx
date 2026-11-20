import { useEffect, useRef } from "react";
import embed from "vega-embed";
import { rows, useDark, useQuery } from "exuvia";

/** Each ticker's 20-day vol in one sector over the last six months. */
export function VolChart({ sector }: { sector: string }) {
  const q = useQuery(`
    select strftime(v.date, '%Y-%m-%d') as day, v.ticker, v.vol_20d
    from rolling_vol v join universe u using (ticker)
    where u.sector = '${sector.replace(/'/g, "''")}'
      and v.date >= (select max(date) from rolling_vol) - interval 6 month
    order by day`);
  const dark = useDark();
  const el = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!el.current || !q.data) return;
    const ink = dark ? "#9b998f" : "#6c6a63";
    const result = embed(
      el.current,
      {
        $schema: "https://vega.github.io/schema/vega-lite/v5.json",
        width: "container",
        height: 260,
        data: { values: rows(q.data) },
        mark: { type: "line", strokeWidth: 1.6 },
        encoding: {
          x: { field: "day", type: "temporal", title: null },
          y: { field: "vol_20d", type: "quantitative", title: null, axis: { format: ".0%" } },
          color: { field: "ticker", type: "nominal", title: null },
        },
        config: {
          background: "transparent",
          view: { stroke: null },
          axis: { labelColor: ink, domainColor: ink, tickColor: ink, gridColor: dark ? "#2a2c31" : "#e2dfd6" },
          legend: { labelColor: ink, orient: "top" },
        },
      },
      { actions: false, renderer: "svg" },
    );
    return () => {
      result.then((r) => r.finalize());
    };
  }, [q.data, dark]);

  if (q.error) return <p className="err">{q.error}</p>;
  return <div ref={el} className="chart" />;
}
