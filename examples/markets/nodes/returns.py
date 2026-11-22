from lib.stats import zscore  # noqa: F401
"""Daily log returns per ticker."""
import exuvia


@exuvia.node()
def returns(prices):
    import numpy as np

    df = prices.sort_values(["ticker", "date"]).copy()
    df["ret"] = df.groupby("ticker")["close"].transform(lambda s: np.log(s).diff())
    return df.dropna(subset=["ret"])[["date", "ticker", "ret"]]
