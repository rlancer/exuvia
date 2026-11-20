"""Daily closes per ticker (synthetic random walk so the demo runs offline).

Swap the body for a real source, e.g. `exuvia.connectors.lobster.query(...)`.
"""
import exuvia


@exuvia.node(schedule="0 17 * * 1-5", tags=["source"])
def prices(universe):
    import numpy as np
    import pandas as pd

    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=260)
    frames = []
    for i, row in universe.iterrows():
        rng = np.random.default_rng(1000 + i)
        drift, vol = rng.uniform(-0.0002, 0.0008), rng.uniform(0.01, 0.03)
        path = 100 * np.exp(np.cumsum(rng.normal(drift, vol, len(dates))))
        frames.append(pd.DataFrame({"date": dates, "ticker": row.ticker, "close": path.round(2)}))
    print(f"generated {len(frames)} tickers x {len(dates)} days")
    return pd.concat(frames, ignore_index=True)
