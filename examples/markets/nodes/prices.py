"""Daily closes per ticker (synthetic random walk so the demo runs offline).

Partitioned by date: the first run backfills every business day since START,
and each later run adds only the days after the newest one it already has,
so the history grows without being refetched. After changing the universe,
run it with a full refresh to rebuild it.

Swap the body for a real source, e.g. `exuvia.connectors.lobster.query(...)`.
"""
import exuvia

START = "2025-01-01"


@exuvia.node(partition_by="date", key=["date", "ticker"], schedule="0 17 * * 1-5", tags=["source"])
def prices(universe):
    import numpy as np
    import pandas as pd

    have = exuvia.partitions()  # dates already stored, oldest first
    dates = pd.bdate_range(START, pd.Timestamp.today().normalize())
    new = dates > pd.Timestamp(have[-1]) if have else np.ones(len(dates), bool)
    if not new.any():
        print("up to date")
        return None
    frames = []
    for i, row in universe.iterrows():
        # The same seed each run, so a day's close never changes.
        rng = np.random.default_rng(1000 + i)
        drift, vol = rng.uniform(-0.0002, 0.0008), rng.uniform(0.01, 0.03)
        path = 100 * np.exp(np.cumsum(rng.normal(drift, vol, len(dates))))
        frames.append(pd.DataFrame({"date": dates.date, "ticker": row.ticker, "close": path.round(2)})[new])
    print(f"generated {len(frames)} tickers x {int(new.sum())} days")
    return pd.concat(frames, ignore_index=True)
