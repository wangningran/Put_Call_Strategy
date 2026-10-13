"""Find cash-secured puts (and covered calls on shares you hold) that pass the entry rules."""

from dataclasses import dataclass
from datetime import date

from . import bs


@dataclass
class Candidate:
    ticker: str
    kind: str
    price: float
    strike: float
    expiry: date
    dte: int
    bid: float
    ask: float
    mid: float
    iv: float
    delta: float
    open_interest: int
    spread_pct: float
    premium: float         # dollars per contract after fees
    collateral: float      # cash tied up per contract (puts)
    ret: float             # premium / capital at risk for this trade
    annualized: float
    breakeven: float
    cushion: float         # how far price can move before breakeven, as a fraction
    earnings: date | None
    earnings_before_expiry: bool


def _evaluate(q, S, today, cfg, earnings, capital_per_contract):
    dte = (q.expiry - today).days
    if q.bid <= 0 or q.ask <= 0 or dte <= 0:
        return None
    mid = q.mid
    spread = (q.ask - q.bid) / mid
    if spread > cfg.max_spread_pct or q.open_interest < cfg.min_open_interest:
        return None
    T = dte / 365
    iv = bs.implied_vol(q.kind, mid, S, q.strike, T, cfg.risk_free_rate) or q.iv
    if not iv or iv <= 0:
        return None
    d = bs.delta(q.kind, S, q.strike, T, cfg.risk_free_rate, iv)
    if not cfg.delta_min <= abs(d) <= cfg.delta_max:
        return None
    premium = mid * 100 - cfg.fee_per_contract
    capital = capital_per_contract(q.strike)
    ret = premium / capital
    if q.kind == "put":
        breakeven = q.strike - mid
        cushion = (S - breakeven) / S
    else:
        breakeven = q.strike + mid
        cushion = (q.strike - S) / S
    before = bool(earnings and today <= earnings <= q.expiry)
    return Candidate(
        ticker=q.ticker, kind=q.kind, price=S, strike=q.strike, expiry=q.expiry, dte=dte,
        bid=q.bid, ask=q.ask, mid=mid, iv=iv, delta=d, open_interest=q.open_interest,
        spread_pct=spread, premium=premium, collateral=q.strike * 100 if q.kind == "put" else 0.0,
        ret=ret, annualized=ret * 365 / dte, breakeven=breakeven, cushion=cushion,
        earnings=earnings, earnings_before_expiry=before,
    )


def _expiries(provider, ticker, today, cfg):
    return [e for e in provider.expirations(ticker) if cfg.dte_min <= (e - today).days <= cfg.dte_max]


def screen_puts(provider, cfg, cash_available, today=None, log=print):
    today = today or date.today()
    out = []
    for ticker in cfg.watchlist:
        try:
            S = provider.price(ticker)
            if S * 100 * 0.85 > cash_available:  # even a ~15% OTM strike would not fit
                log(f"  {ticker}: 现价 ${S:.2f}，一张合约需约 ${S*100:,.0f}，超出可用现金 ${cash_available:,.0f}，跳过")
                continue
            earnings = provider.next_earnings(ticker)
            for exp in _expiries(provider, ticker, today, cfg):
                for q in provider.chain(ticker, exp, "put"):
                    if q.strike >= S or q.strike * 100 > cash_available:
                        continue
                    if q.strike * 100 > cfg.account_size * cfg.max_ticker_pct:
                        continue
                    c = _evaluate(q, S, today, cfg, earnings, lambda k: k * 100)
                    if c and not (cfg.avoid_earnings and c.earnings_before_expiry):
                        out.append(c)
        except Exception as e:  # one bad ticker should not kill the scan
            log(f"  {ticker}: 获取失败（{e}）")
    return sorted(out, key=lambda c: c.annualized, reverse=True)


def screen_calls(provider, cfg, positions, today=None, log=print):
    """Covered calls for every ticker where you hold at least 100 uncovered shares.
    positions: {ticker: {"shares", "avg_cost", "uncovered"}}"""
    today = today or date.today()
    out = []
    for ticker, p in positions.items():
        lots = p.get("uncovered", p["shares"]) // 100
        if lots < 1:
            continue
        try:
            S = provider.price(ticker)
            earnings = provider.next_earnings(ticker)
            for exp in _expiries(provider, ticker, today, cfg):
                for q in provider.chain(ticker, exp, "call"):
                    if q.strike <= S:
                        continue
                    if cfg.calls_above_cost_basis and q.strike < p["avg_cost"]:
                        continue
                    c = _evaluate(q, S, today, cfg, earnings, lambda k: S * 100)
                    if c and not (cfg.avoid_earnings and c.earnings_before_expiry):
                        out.append(c)
        except Exception as e:
            log(f"  {ticker}: 获取失败（{e}）")
    return sorted(out, key=lambda c: c.annualized, reverse=True)
