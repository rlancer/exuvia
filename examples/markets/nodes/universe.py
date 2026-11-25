"""The tickers this project tracks, with sector labels."""
import exuvia


@exuvia.node(key="ticker", tags=["source"])
def universe():
    import pandas as pd

    return pd.DataFrame(
        {
            "ticker": ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "UNH", "KO", "CAT"],
            "sector": ["Tech", "Tech", "Tech", "Financials", "Energy", "Health", "Staples", "Industrials"],
        }
    )
