"""Load config.toml with defaults for anything left out."""

from dataclasses import dataclass, field, fields
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # account
    account_size: float = 10_000.0
    cash_reserve_pct: float = 0.20        # never commit this share of the account
    max_ticker_pct: float = 0.50          # max collateral + stock value in one ticker
    fee_per_contract: float = 0.65        # commission + regulatory fees, per contract per side
    risk_free_rate: float = 0.04
    # entry rules
    dte_min: int = 25
    dte_max: int = 45
    delta_min: float = 0.15
    delta_max: float = 0.30
    min_open_interest: int = 100
    max_spread_pct: float = 0.15          # (ask - bid) / mid
    avoid_earnings: bool = True
    calls_above_cost_basis: bool = True   # covered calls only at or above cost basis
    # management rules
    take_profit: float = 0.50             # buy back once 50% of the premium is captured
    roll_dte: int = 21
    loss_multiple: float = 2.0            # alert when loss >= 2x premium received
    near_strike_pct: float = 0.02         # alert when price is within 2% of strike
    # universe
    watchlist: list = field(default_factory=lambda: ["SPY"])
    # files
    journal_path: str = "data/journal.csv"
    stock_ledger_path: str = "data/stock_ledger.csv"

    def path(self, name):
        p = Path(getattr(self, name))
        return p if p.is_absolute() else ROOT / p


def load(path=None):
    path = Path(path) if path else ROOT / "config.toml"
    cfg = Config()
    if not path.exists():
        return cfg
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    flat = {}
    for key, value in raw.items():
        if isinstance(value, dict) and key != "watchlist":
            flat.update(value)
        else:
            flat[key] = value
    if isinstance(flat.get("watchlist"), dict):
        flat["watchlist"] = flat["watchlist"].get("tickers", [])
    if "tickers" in flat:
        flat["watchlist"] = flat.pop("tickers")
    known = {f.name for f in fields(Config)}
    unknown = set(flat) - known
    if unknown:
        raise ValueError(f"Unknown config keys in {path}: {sorted(unknown)}")
    for key, value in flat.items():
        setattr(cfg, key, value)
    return cfg
