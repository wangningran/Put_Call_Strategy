"""Trade journal: one CSV row per option trade, plus a stock ledger fed by assignments.

Option P&L per trade = (premium - close_price) * 100 * contracts - fees.
Expired and assigned options close at 0; an assignment also books the shares at the strike.
"""

import csv
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date

OPEN, CLOSED, EXPIRED, ASSIGNED = "OPEN", "CLOSED", "EXPIRED", "ASSIGNED"


@dataclass
class OptionTrade:
    id: int
    open_date: str
    ticker: str
    kind: str              # put / call
    strike: float
    expiry: str
    contracts: int
    premium: float         # per share received when selling
    open_fees: float
    close_date: str = ""
    close_price: float = 0.0
    close_fees: float = 0.0
    status: str = OPEN
    notes: str = ""

    @property
    def expiry_date(self):
        return date.fromisoformat(self.expiry)

    @property
    def collateral(self):
        """Cash a cash-secured put ties up. Covered calls are secured by shares instead."""
        return self.strike * 100 * self.contracts if self.kind == "put" else 0.0

    @property
    def realized_pnl(self):
        if self.status == OPEN:
            return 0.0
        return (self.premium - self.close_price) * 100 * self.contracts - self.open_fees - self.close_fees


@dataclass
class StockFill:
    date: str
    ticker: str
    shares: int            # + bought, - sold
    price: float
    fees: float
    source: str            # e.g. "assigned #3" or "manual"


def _read(path, cls):
    if not path.exists():
        return []
    types = {f.name: f.type for f in fields(cls)}
    conv = {"int": int, "float": float, "str": str, int: int, float: float, str: str}
    with open(path, newline="") as f:
        return [cls(**{k: conv[types[k]](v) for k, v in row.items()}) for row in csv.DictReader(f)]


def _write(path, rows, cls):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[f.name for f in fields(cls)])
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


class Journal:
    def __init__(self, trades_path, ledger_path):
        self.trades_path = trades_path
        self.ledger_path = ledger_path
        self.trades = _read(trades_path, OptionTrade)
        self.fills = _read(ledger_path, StockFill)

    def save(self):
        _write(self.trades_path, self.trades, OptionTrade)
        _write(self.ledger_path, self.fills, StockFill)

    def get(self, trade_id):
        for t in self.trades:
            if t.id == trade_id:
                return t
        raise KeyError(f"No trade #{trade_id}")

    # --- recording -------------------------------------------------------

    def open(self, ticker, kind, strike, expiry, contracts, premium, fees, on=None, notes=""):
        kind = kind.lower()
        if kind not in ("put", "call"):
            raise ValueError("kind must be put or call")
        if kind == "call":
            held = self.positions().get(ticker.upper(), {}).get("shares", 0)
            covered = sum(t.contracts for t in self.open_trades() if t.ticker == ticker.upper() and t.kind == "call")
            if held < (covered + contracts) * 100:
                raise ValueError(f"{ticker.upper()} 只持有 {held} 股，这张 Call 会变成裸卖，已拒绝")
        trade = OptionTrade(
            id=max((t.id for t in self.trades), default=0) + 1,
            open_date=(on or date.today()).isoformat(), ticker=ticker.upper(), kind=kind,
            strike=float(strike), expiry=date.fromisoformat(str(expiry)).isoformat(),
            contracts=int(contracts), premium=float(premium), open_fees=float(fees), notes=notes,
        )
        self.trades.append(trade)
        return trade

    def _close(self, trade_id, status, price, fees, on):
        t = self.get(trade_id)
        if t.status != OPEN:
            raise ValueError(f"Trade #{trade_id} is already {t.status}")
        t.status, t.close_price, t.close_fees = status, float(price), float(fees)
        t.close_date = (on or date.today()).isoformat()
        return t

    def close(self, trade_id, price, fees=0.0, on=None):
        return self._close(trade_id, CLOSED, price, fees, on)

    def expire(self, trade_id, on=None):
        t = self.get(trade_id)
        return self._close(trade_id, EXPIRED, 0.0, 0.0, on or t.expiry_date)

    def assign(self, trade_id, fees=0.0, on=None):
        t = self.get(trade_id)
        on = on or t.expiry_date
        self._close(trade_id, ASSIGNED, 0.0, 0.0, on)
        shares = 100 * t.contracts * (1 if t.kind == "put" else -1)
        self.fills.append(StockFill(on.isoformat(), t.ticker, shares, t.strike, float(fees), f"assigned #{t.id}"))
        return t

    def stock(self, ticker, shares, price, fees=0.0, on=None):
        fill = StockFill((on or date.today()).isoformat(), ticker.upper(), int(shares), float(price), float(fees), "manual")
        self.fills.append(fill)
        return fill

    # --- views -----------------------------------------------------------

    def open_trades(self):
        return [t for t in self.trades if t.status == OPEN]

    def positions(self):
        """Average-cost stock positions plus realized stock P&L per ticker.
        net_basis subtracts every option premium collected on the ticker (the wheel view)."""
        pos = defaultdict(lambda: {"shares": 0, "cost": 0.0, "realized": 0.0})
        for f in sorted(self.fills, key=lambda f: f.date):
            p = pos[f.ticker]
            if f.shares > 0:
                p["cost"] += f.shares * f.price + f.fees
                p["shares"] += f.shares
            else:
                avg = p["cost"] / p["shares"] if p["shares"] else 0.0
                sold = -f.shares
                p["realized"] += sold * (f.price - avg) - f.fees
                p["cost"] -= sold * avg
                p["shares"] -= sold
        out = {}
        for ticker, p in pos.items():
            avg = p["cost"] / p["shares"] if p["shares"] else 0.0
            premiums = sum(t.realized_pnl for t in self.trades if t.ticker == ticker and t.status != OPEN)
            out[ticker] = {
                "shares": p["shares"], "avg_cost": avg, "realized": p["realized"],
                "net_basis": (p["cost"] - premiums) / p["shares"] if p["shares"] else 0.0,
            }
        return out

    def stats(self):
        closed = [t for t in self.trades if t.status != OPEN]
        wins = [t for t in closed if t.realized_pnl > 0]
        monthly = defaultdict(float)
        for t in closed:
            monthly[t.close_date[:7]] += t.realized_pnl
        pos = self.positions()
        return {
            "closed_trades": len(closed),
            "open_trades": len(self.open_trades()),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "option_pnl": sum(t.realized_pnl for t in closed),
            "stock_pnl": sum(p["realized"] for p in pos.values()),
            "assignments": sum(1 for t in closed if t.status == ASSIGNED),
            "monthly": dict(sorted(monthly.items())),
            "collateral": sum(t.collateral for t in self.open_trades()),
        }
