"""Wheel strategy backtest on daily closes.

Free historical option prices are not available, so option prices are modelled with
Black-Scholes using IV = trailing realized volatility x iv_multiplier. That ignores put skew
and the IV spikes around earnings and crashes, so treat results as a rough sanity check of the
rules, not as a forecast. Real fills, skew and gaps will usually make live results worse.
"""

import math
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from . import bs


@dataclass
class Params:
    initial_cash: float = 10_000.0
    put_delta: float = 0.25
    call_delta: float = 0.25
    dte: int = 35
    take_profit: float = 0.50
    iv_multiplier: float = 1.10       # implied vol usually trades above realized vol
    vol_window: int = 20
    min_iv: float = 0.10
    fee_per_contract: float = 0.65
    slippage_pct: float = 0.05        # share of the option price lost to the bid/ask spread each fill
    cash_reserve_pct: float = 0.0
    risk_free_rate: float = 0.04
    calls_above_cost_basis: bool = True
    min_premium: float = 0.05         # skip trades paying less than this per share


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame
    metrics: dict = field(default_factory=dict)


def _max_drawdown(s):
    return float((s / s.cummax() - 1).min())


def run(closes, p=None):
    p = p or Params()
    closes = closes.dropna().astype(float)
    rv = np.log(closes).diff().rolling(p.vol_window).std() * math.sqrt(252)

    cash, shares, basis = p.initial_cash, 0, 0.0
    opt = None
    trades, equity = [], []

    for i, (day, S) in enumerate(closes.items()):
        if math.isnan(rv.iloc[i]):
            equity.append(cash + shares * S)
            continue
        sigma = max(rv.iloc[i] * p.iv_multiplier, p.min_iv)

        # 1. manage the open option: settle at expiry or take profit
        if opt:
            n, K = opt["n"], opt["strike"]
            if day >= opt["expiry"]:
                assigned = (S < K) if opt["kind"] == "put" else (S > K)
                if assigned and opt["kind"] == "put":
                    cash -= K * 100 * n
                    basis = (basis * shares + K * 100 * n) / (shares + 100 * n)
                    shares += 100 * n
                elif assigned:
                    cash += K * 100 * n
                    opt["stock_pnl"] = (K - basis) * 100 * n
                    shares -= 100 * n
                    if shares == 0:
                        basis = 0.0
                opt.update(close_date=day, close_price=0.0, outcome="assigned" if assigned else "expired")
            else:
                T = (opt["expiry"] - day).days / 365
                value = bs.price(opt["kind"], S, K, T, p.risk_free_rate, sigma)
                if value <= opt["premium"] * (1 - p.take_profit):
                    fill = value * (1 + p.slippage_pct)
                    cash -= fill * 100 * n + p.fee_per_contract * n
                    opt.update(close_date=day, close_price=fill, outcome="closed", fees=opt["fees"] + p.fee_per_contract * n)
            if "outcome" in opt:
                opt["pnl"] = (opt["premium"] - opt["close_price"]) * 100 * n - opt["fees"]
                trades.append(opt)
                opt = None

        # 2. open a new option if flat
        if opt is None:
            T = p.dte / 365
            if shares >= 100:
                kind, n = "call", shares // 100
                K = bs.strike_for_delta("call", S, T, p.risk_free_rate, sigma, p.call_delta)
                if p.calls_above_cost_basis and K < basis:
                    inc = bs.strike_increment(S)
                    K = math.ceil(basis / inc) * inc
            else:
                kind = "put"
                K = bs.strike_for_delta("put", S, T, p.risk_free_rate, sigma, p.put_delta)
                n = int(cash * (1 - p.cash_reserve_pct) // (K * 100)) if K > 0 else 0
            prem = bs.price(kind, S, K, T, p.risk_free_rate, sigma) * (1 - p.slippage_pct)
            if n > 0 and prem >= p.min_premium:
                fees = p.fee_per_contract * n
                cash += prem * 100 * n - fees
                opt = {"open_date": day, "kind": kind, "strike": K, "expiry": day + timedelta(days=p.dte),
                       "n": n, "premium": prem, "fees": fees, "price_at_open": S, "stock_pnl": 0.0}

        liability = 0.0
        if opt:
            T = max((opt["expiry"] - day).days, 0) / 365
            liability = bs.price(opt["kind"], S, opt["strike"], T, p.risk_free_rate, sigma) * 100 * opt["n"]
        equity.append(cash + shares * S - liability)

    eq = pd.Series(equity, index=closes.index, name="wheel")
    tr = pd.DataFrame(trades)
    return Result(eq, tr, _metrics(eq, closes, tr, p))


def _metrics(eq, closes, tr, p):
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    bh = closes / closes.iloc[0] * p.initial_cash
    monthly = eq.resample("ME").last().pct_change().dropna()
    m = {
        "start": eq.index[0].date().isoformat(),
        "end": eq.index[-1].date().isoformat(),
        "final_equity": float(eq.iloc[-1]),
        "total_return": float(eq.iloc[-1] / p.initial_cash - 1),
        "cagr": float((eq.iloc[-1] / p.initial_cash) ** (1 / years) - 1),
        "max_drawdown": _max_drawdown(eq),
        "worst_month": float(monthly.min()) if len(monthly) else 0.0,
        "bh_total_return": float(bh.iloc[-1] / p.initial_cash - 1),
        "bh_cagr": float((bh.iloc[-1] / p.initial_cash) ** (1 / years) - 1),
        "bh_max_drawdown": _max_drawdown(bh),
        "option_trades": len(tr),
    }
    if len(tr):
        m.update(
            option_win_rate=float((tr["pnl"] > 0).mean()),
            put_assignments=int(((tr["kind"] == "put") & (tr["outcome"] == "assigned")).sum()),
            calls_called_away=int(((tr["kind"] == "call") & (tr["outcome"] == "assigned")).sum()),
            net_premium=float(tr["pnl"].sum()),
            avg_monthly_premium=float(tr["pnl"].sum() / (years * 12)),
        )
    return m


def synthetic_prices(days=1000, mu=0.08, sigma=0.25, start=100.0, seed=0, crash_at=None, crash=-0.3):
    """Geometric Brownian motion closes on business days, optional one-day crash. For tests and demos."""
    rng = np.random.default_rng(seed)
    dt = 1 / 252
    rets = rng.normal((mu - 0.5 * sigma ** 2) * dt, sigma * math.sqrt(dt), days)
    if crash_at is not None:
        rets[crash_at] += math.log(1 + crash)
    idx = pd.bdate_range("2020-01-01", periods=days)
    return pd.Series(start * np.exp(np.cumsum(rets)), index=idx, name="Close")
