"""Market data providers. Anything with the same methods as YahooProvider can be swapped in
(a moomoo OpenD provider is the planned next one)."""

from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd


@dataclass
class OptionQuote:
    ticker: str
    kind: str          # 'put' or 'call'
    strike: float
    expiry: date
    bid: float
    ask: float
    last: float
    iv: float          # provider-reported IV, may be unreliable outside market hours
    open_interest: int
    volume: int

    @property
    def mid(self):
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last


def _num(x, default=0):
    try:
        if x is None or pd.isna(x):
            return default
        return x
    except TypeError:
        return x


class YahooProvider:
    """Free, roughly 15-minute delayed data via yfinance."""

    def __init__(self):
        import yfinance as yf
        self._yf = yf
        self._tickers = {}

    def _t(self, ticker):
        if ticker not in self._tickers:
            self._tickers[ticker] = self._yf.Ticker(ticker)
        return self._tickers[ticker]

    def price(self, ticker):
        hist = self._t(ticker).history(period="5d")
        if hist.empty:
            raise RuntimeError(f"No price data for {ticker}")
        return float(hist["Close"].iloc[-1])

    def expirations(self, ticker):
        return [datetime.strptime(s, "%Y-%m-%d").date() for s in self._t(ticker).options]

    def chain(self, ticker, expiry, kind):
        oc = self._t(ticker).option_chain(expiry.strftime("%Y-%m-%d"))
        df = oc.puts if kind == "put" else oc.calls
        return [
            OptionQuote(
                ticker=ticker, kind=kind, strike=float(r.strike), expiry=expiry,
                bid=float(_num(r.bid)), ask=float(_num(r.ask)), last=float(_num(r.lastPrice)),
                iv=float(_num(r.impliedVolatility)),
                open_interest=int(_num(r.openInterest)), volume=int(_num(r.volume)),
            )
            for r in df.itertuples()
        ]

    def next_earnings(self, ticker):
        try:
            cal = self._t(ticker).calendar
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if dates:
                d = dates[0]
                return d.date() if isinstance(d, datetime) else d
        except Exception:
            pass
        return None

    def history(self, ticker, start, end=None):
        hist = self._t(ticker).history(start=start, end=end, auto_adjust=True)
        if hist.empty:
            raise RuntimeError(f"No history for {ticker}")
        s = hist["Close"]
        s.index = s.index.tz_localize(None).normalize()
        return s


def load_csv_history(path):
    """CSV with Date and Close (or Adj Close) columns, e.g. exported from Yahoo or moomoo."""
    df = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
    col = "Adj Close" if "Adj Close" in df.columns else "Close"
    return df[col].astype(float)
