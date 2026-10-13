"""Check open positions against the management rules and produce alerts."""

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import date


@dataclass
class Alert:
    level: str        # ACTION / WARN / INFO
    trade_id: int | None
    ticker: str
    message: str

    def __str__(self):
        ref = f"#{self.trade_id} " if self.trade_id else ""
        return f"[{self.level}] {ref}{self.ticker}: {self.message}"


def _quote(provider, trade):
    for q in provider.chain(trade.ticker, trade.expiry_date, trade.kind):
        if abs(q.strike - trade.strike) < 1e-6:
            return q
    return None


def check(provider, journal, cfg, today=None):
    """Returns (alerts, rows). rows hold per-position numbers for display."""
    today = today or date.today()
    alerts, rows = [], []
    prices = {}

    def price(t):
        if t not in prices:
            prices[t] = provider.price(t)
        return prices[t]

    for t in journal.open_trades():
        dte = (t.expiry_date - today).days
        if dte < 0:
            alerts.append(Alert("ACTION", t.id, t.ticker,
                                f"已于 {t.expiry} 到期，请记录结果：`log expire {t.id}`（作废）或 `log assign {t.id}`（被行权）"))
            continue
        try:
            S = price(t.ticker)
            q = _quote(provider, t)
        except Exception as e:
            alerts.append(Alert("WARN", t.id, t.ticker, f"获取报价失败（{e}）"))
            continue
        mid = q.mid if q else None
        captured = (t.premium - mid) / t.premium if mid is not None else None
        itm = S < t.strike if t.kind == "put" else S > t.strike
        dist = (S - t.strike) / S if t.kind == "put" else (t.strike - S) / S
        rows.append({"id": t.id, "ticker": t.ticker, "kind": t.kind, "strike": t.strike, "expiry": t.expiry,
                     "dte": dte, "price": S, "premium": t.premium, "mid": mid, "captured": captured, "itm": itm})
        label = f"{t.kind} {t.strike:g} {t.expiry}"

        if captured is not None and captured >= cfg.take_profit:
            alerts.append(Alert("ACTION", t.id, t.ticker,
                                f"{label} 已赚到权利金的 {captured:.0%}，建议以约 ${mid:.2f} 买回平仓"))
        elif mid is not None and (mid - t.premium) >= cfg.loss_multiple * t.premium:
            alerts.append(Alert("ACTION", t.id, t.ticker,
                                f"{label} 亏损已达权利金的 {(mid - t.premium) / t.premium:.1f} 倍，按止损规则平仓或展期"))
        elif dte <= cfg.roll_dte:
            alerts.append(Alert("WARN", t.id, t.ticker,
                                f"{label} 还剩 {dte} 天，只赚到 "
                                f"{(captured or 0):.0%}，考虑平仓或展期"))
        if itm:
            what = "很可能被行权，准备好接股" if t.kind == "put" else "正股很可能被叫走"
            alerts.append(Alert("WARN", t.id, t.ticker, f"{label} 已价内（现价 ${S:.2f}），{what}"))
        elif dist < cfg.near_strike_pct:
            alerts.append(Alert("WARN", t.id, t.ticker, f"{label}：现价 ${S:.2f} 距行权价只有 {dist:.1%}"))
        try:
            e = provider.next_earnings(t.ticker)
        except Exception:
            e = None
        if e and today <= e <= t.expiry_date:
            alerts.append(Alert("WARN", t.id, t.ticker, f"财报日 {e} 在到期日 {t.expiry} 之前，注意跳空风险"))

    # portfolio-level checks
    exposure = {}
    for t in journal.open_trades():
        exposure[t.ticker] = exposure.get(t.ticker, 0.0) + t.collateral
    for ticker, p in journal.positions().items():
        if p["shares"]:
            try:
                exposure[ticker] = exposure.get(ticker, 0.0) + p["shares"] * price(ticker)
            except Exception:
                exposure[ticker] = exposure.get(ticker, 0.0) + p["shares"] * p["avg_cost"]
    committed = sum(exposure.values())
    limit = cfg.account_size * (1 - cfg.cash_reserve_pct)
    if committed > limit:
        alerts.append(Alert("WARN", None, "PORTFOLIO",
                            f"已占用 ${committed:,.0f}，超过上限 ${limit:,.0f}（现金储备 {cfg.cash_reserve_pct:.0%}）"))
    for ticker, amount in exposure.items():
        if amount > cfg.account_size * cfg.max_ticker_pct:
            alerts.append(Alert("WARN", None, ticker,
                                f"敞口 ${amount:,.0f}，占账户 {amount / cfg.account_size:.0%}（上限 {cfg.max_ticker_pct:.0%}）"))
    return alerts, rows


def notify_telegram(text):
    """Send text to Telegram if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set. Returns True when sent."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=15)
    return True
