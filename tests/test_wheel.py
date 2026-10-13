import math
from datetime import date, timedelta

import pytest

from wheel import bs, backtest as bt
from wheel.config import Config, load
from wheel.data import OptionQuote
from wheel.journal import Journal
from wheel.monitor import check
from wheel.screener import screen_calls, screen_puts

TODAY = date(2026, 10, 13)
R = 0.04


class FakeProvider:
    """Chain priced with Black-Scholes at a flat IV, spread of 4% around the model price."""

    def __init__(self, prices, iv=0.35, earnings=None):
        self.prices, self.iv, self.earnings = prices, iv, earnings or {}

    def price(self, t):
        return self.prices[t]

    def expirations(self, t):
        return [TODAY + timedelta(days=d) for d in (7, 30, 37, 60)]

    def chain(self, t, expiry, kind):
        S = self.prices[t]
        T = (expiry - TODAY).days / 365
        out = []
        k = math.floor(S * 0.7)
        while k <= S * 1.3:
            mid = bs.price(kind, S, k, T, R, self.iv)
            if mid > 0.02:
                out.append(OptionQuote(t, kind, float(k), expiry, mid * 0.98, mid * 1.02, mid, self.iv, 500, 50))
            k += 1
        return out

    def next_earnings(self, t):
        return self.earnings.get(t)


def test_put_call_parity_and_iv_roundtrip():
    S, K, T, sig = 50, 48, 0.1, 0.3
    c, p = bs.price("call", S, K, T, R, sig), bs.price("put", S, K, T, R, sig)
    assert c - p == pytest.approx(S - K * math.exp(-R * T), abs=1e-9)
    assert bs.implied_vol("put", p, S, K, T, R) == pytest.approx(sig, abs=1e-4)
    assert -1 < bs.delta("put", S, K, T, R, sig) < 0 < bs.delta("call", S, K, T, R, sig) < 1


def test_strike_for_delta_is_otm_and_close_to_target():
    S, T, sig = 40, 35 / 365, 0.4
    kp = bs.strike_for_delta("put", S, T, R, sig, 0.25)
    kc = bs.strike_for_delta("call", S, T, R, sig, 0.25)
    assert kp < S < kc
    assert abs(bs.delta("put", S, kp, T, R, sig)) <= 0.25 + 1e-9
    assert abs(bs.delta("put", S, kp + 0.5, T, R, sig)) > 0.2


def test_journal_wheel_cycle(tmp_path):
    j = Journal(tmp_path / "j.csv", tmp_path / "l.csv")
    put = j.open("sofi", "put", 20, "2026-11-20", 2, 0.50, 1.30, on=TODAY)
    with pytest.raises(ValueError):
        j.open("SOFI", "call", 22, "2026-12-18", 1, 0.40, 0.65)  # no shares yet: naked
    j.assign(put.id)
    assert put.realized_pnl == pytest.approx(0.5 * 200 - 1.30)
    assert j.positions()["SOFI"]["shares"] == 200
    call = j.open("SOFI", "call", 21, "2026-12-18", 2, 0.40, 1.30)
    j.assign(call.id)
    j.save()

    j2 = Journal(tmp_path / "j.csv", tmp_path / "l.csv")  # round-trips through CSV
    pos = j2.positions()["SOFI"]
    assert pos["shares"] == 0
    assert pos["realized"] == pytest.approx(200 * (21 - 20))
    s = j2.stats()
    assert s["closed_trades"] == 2 and s["assignments"] == 2 and s["win_rate"] == 1.0
    assert s["option_pnl"] == pytest.approx(100 - 1.3 + 80 - 1.3)


def test_journal_close_and_expire(tmp_path):
    j = Journal(tmp_path / "j.csv", tmp_path / "l.csv")
    a = j.open("F", "put", 11, "2026-11-20", 1, 0.30, 0.65, on=TODAY)
    b = j.open("F", "put", 10, "2026-11-20", 1, 0.20, 0.65, on=TODAY)
    j.close(a.id, 0.15, 0.65)
    j.expire(b.id)
    assert a.realized_pnl == pytest.approx(15 - 1.30)
    assert b.realized_pnl == pytest.approx(20 - 0.65)
    with pytest.raises(ValueError):
        j.expire(b.id)


def test_screen_puts_respects_rules():
    cfg = Config(watchlist=["ABC", "BIG"], min_open_interest=100)
    prov = FakeProvider({"ABC": 30.0, "BIG": 400.0})
    cands = screen_puts(prov, cfg, cash_available=8000, today=TODAY, log=lambda *_: None)
    assert cands, "expected candidates"
    for c in cands:
        assert c.ticker == "ABC"                       # BIG is unaffordable
        assert cfg.dte_min <= c.dte <= cfg.dte_max     # 7 and 60 DTE excluded
        assert cfg.delta_min <= abs(c.delta) <= cfg.delta_max
        assert c.strike < c.price and c.strike * 100 <= 8000
    assert cands == sorted(cands, key=lambda c: c.annualized, reverse=True)


def test_screen_skips_earnings():
    cfg = Config(watchlist=["ABC"])
    prov = FakeProvider({"ABC": 30.0}, earnings={"ABC": TODAY + timedelta(days=33)})
    cands = screen_puts(prov, cfg, 8000, today=TODAY, log=lambda *_: None)
    assert cands and all(c.dte < 33 for c in cands)


def test_screen_calls_above_cost_basis():
    cfg = Config()
    prov = FakeProvider({"ABC": 30.0})
    cands = screen_calls(prov, cfg, {"ABC": {"shares": 100, "avg_cost": 32.0, "uncovered": 100}}, today=TODAY,
                         log=lambda *_: None)
    assert all(c.strike >= 32 for c in cands)
    assert screen_calls(prov, cfg, {"ABC": {"shares": 100, "avg_cost": 25, "uncovered": 0}}, today=TODAY) == []


def test_monitor_alerts(tmp_path):
    cfg = Config(account_size=10_000)
    j = Journal(tmp_path / "j.csv", tmp_path / "l.csv")
    exp = (TODAY + timedelta(days=30)).isoformat()
    j.open("ABC", "put", 25, exp, 1, 2.00, 0.65, on=TODAY)       # far OTM now, mostly decayed -> take profit
    j.open("XYZ", "put", 30, exp, 1, 0.50, 0.65, on=TODAY)       # deep ITM -> loss + ITM
    j.open("ABC", "put", 20, "2026-10-01", 1, 0.40, 0.65)       # already expired
    prov = FakeProvider({"ABC": 30.0, "XYZ": 24.0})
    alerts, rows = check(prov, j, cfg, today=TODAY)
    assert any(a.trade_id == 1 and "已赚到" in a.message for a in alerts)
    assert any(a.trade_id == 2 and "价内" in a.message for a in alerts)
    assert any(a.trade_id == 2 and "倍" in a.message for a in alerts)
    assert any(a.trade_id == 3 and "到期" in a.message for a in alerts)
    assert len(rows) == 2


def test_backtest_runs_and_stays_cash_secured():
    closes = bt.synthetic_prices(days=800, start=40, seed=1, crash_at=400, crash=-0.25)
    res = bt.run(closes, bt.Params(initial_cash=10_000))
    m = res.metrics
    assert m["option_trades"] > 10
    assert (res.equity > 0).all()
    assert m["max_drawdown"] < 0
    assert set(res.trades["outcome"]) <= {"closed", "expired", "assigned"}
    # never more contracts than cash (puts) or shares (calls) can cover
    puts = res.trades[res.trades["kind"] == "put"]
    assert (puts["strike"] * 100 * puts["n"] <= 10_000 * 1.5).all()


def test_backtest_unaffordable_underlying_does_nothing():
    closes = bt.synthetic_prices(days=300, start=500, seed=2)
    res = bt.run(closes, bt.Params(initial_cash=10_000))
    assert res.metrics["option_trades"] == 0
    assert res.equity.iloc[-1] == pytest.approx(10_000)


def test_shipped_config_loads():
    cfg = load()
    assert cfg.account_size == 10_000 and len(cfg.watchlist) > 3
