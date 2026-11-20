"""20-day annualized realized volatility per ticker."""
import exuvia


@exuvia.node(
    chart={
        "mark": {"type": "line", "strokeWidth": 1.5},
        "encoding": {
            "x": {"field": "date", "type": "temporal", "title": None},
            "y": {"field": "vol_20d", "type": "quantitative", "title": "20d vol", "axis": {"format": ".0%"}},
            "color": {"field": "ticker", "type": "nominal"},
        },
    }
)
def rolling_vol(returns):
    out = returns.sort_values("date").copy()
    out["vol_20d"] = out.groupby("ticker")["ret"].transform(lambda s: s.rolling(20).std() * 252**0.5)
    return out.dropna(subset=["vol_20d"])[["date", "ticker", "vol_20d"]]
