import { useState } from "react";
import { rows, useQuery, useTable } from "exuvia";
import { VolChart } from "./VolChart";
import { pct } from "./format";
import "./styles.css";

interface Sector {
  sector: string;
  tickers: number;
  avg_vol: number;
  avg_ret_1m: number;
}

interface Latest {
  ticker: string;
  sector: string;
  vol_20d: number;
}

export default function App() {
  const sectors = useTable("sector_summary");
  // The query sees only the nodes in view.json's "reads".
  const latest = useQuery(`
    select v.ticker, u.sector, v.vol_20d
    from rolling_vol v join universe u using (ticker)
    qualify row_number() over (partition by v.ticker order by v.date desc) = 1
    order by v.vol_20d desc`);
  const [picked, setPicked] = useState<string | null>(null);

  const bySector = rows<Sector>(sectors.data);
  const tickers = rows<Latest>(latest.data);
  const sector = picked ?? bySector[0]?.sector ?? null;
  const top = Math.max(...bySector.map((s) => s.avg_vol), 0);
  const error = sectors.error ?? latest.error;

  return (
    <main className="board">
      {error && <p className="err">{error}</p>}
      <section className="kpis">
        <Kpi label="Tickers" value={String(tickers.length || "—")} />
        <Kpi label="Highest-vol sector" value={bySector[0]?.sector ?? "—"} sub={bySector[0] ? pct(bySector[0].avg_vol) : ""} />
        <Kpi label="Highest-vol ticker" value={tickers[0]?.ticker ?? "—"} sub={tickers[0] ? pct(tickers[0].vol_20d) : ""} />
      </section>

      <section className="cols">
        <div className="card">
          <h2>Sectors by average 20d vol</h2>
          <table>
            <thead>
              <tr><th>Sector</th><th className="num">Tickers</th><th>Avg vol</th><th className="num">1m return</th></tr>
            </thead>
            <tbody>
              {bySector.map((s) => (
                <tr key={s.sector} className={s.sector === sector ? "on" : ""} onClick={() => setPicked(s.sector)}>
                  <td>{s.sector}</td>
                  <td className="num">{s.tickers}</td>
                  <td>
                    <span className="bar" style={{ width: `${top ? (s.avg_vol / top) * 100 : 0}%` }} />
                    <span className="num">{pct(s.avg_vol)}</span>
                  </td>
                  <td className={`num ${s.avg_ret_1m < 0 ? "neg" : "pos"}`}>{pct(s.avg_ret_1m)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="hint">Click a sector to chart it.</p>
        </div>

        <div className="card">
          <h2>{sector ? `${sector}: 20d vol` : "20d vol"}</h2>
          {sector && <VolChart sector={sector} />}
        </div>
      </section>
    </main>
  );
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="kpi">
      <span className="label">{label}</span>
      <span className="value">{value}</span>
      {sub && <span className="sub">{sub}</span>}
    </div>
  );
}
