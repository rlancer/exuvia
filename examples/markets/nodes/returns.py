"""Daily log returns per ticker."""
import exuvia


@exuvia.node(key=["date", "ticker"])
def returns(prices):
    import numpy as np

    df = prices.sort_values(["ticker", "date"]).copy()
    df["ret"] = df.groupby("ticker")["close"].transform(lambda s: np.log(s).diff())
    return df.dropna(subset=["ret"])[["date", "ticker", "ret"]].reset_index(drop=True)
