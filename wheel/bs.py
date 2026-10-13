"""Black-Scholes pricing, delta, implied volatility and strike-from-delta helpers."""

import math
from statistics import NormalDist

_N = NormalDist()


def _d1(S, K, T, r, sigma):
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def price(kind, S, K, T, r, sigma):
    """Option price per share. kind is 'put' or 'call'; T in years."""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0) if kind == "put" else max(S - K, 0.0)
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    disc = math.exp(-r * T)
    if kind == "call":
        return S * _N.cdf(d1) - K * disc * _N.cdf(d2)
    return K * disc * _N.cdf(-d2) - S * _N.cdf(-d1)


def delta(kind, S, K, T, r, sigma):
    """Delta per share: call in (0, 1), put in (-1, 0)."""
    if T <= 0 or sigma <= 0:
        if kind == "call":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = _d1(S, K, T, r, sigma)
    return _N.cdf(d1) if kind == "call" else _N.cdf(d1) - 1.0


def implied_vol(kind, opt_price, S, K, T, r, lo=1e-4, hi=5.0, tol=1e-6):
    """Bisection IV solver. Returns None when the price is outside the arbitrage bounds."""
    if T <= 0 or opt_price <= 0:
        return None
    if opt_price < price(kind, S, K, T, r, lo) or opt_price > price(kind, S, K, T, r, hi):
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if price(kind, S, K, T, r, mid) > opt_price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def strike_increment(S):
    """Typical listed strike spacing for US equity options."""
    if S < 25:
        return 0.5
    if S < 200:
        return 1.0
    return 5.0


def strike_for_delta(kind, S, T, r, sigma, target_abs_delta):
    """Strike whose |delta| is closest to the target, rounded further out of the money
    to a listed strike (puts round down, calls round up)."""
    if kind == "put":
        d1 = _N.inv_cdf(1.0 - target_abs_delta)
    else:
        d1 = _N.inv_cdf(target_abs_delta)
    K = S * math.exp(-(d1 * sigma * math.sqrt(T) - (r + 0.5 * sigma * sigma) * T))
    inc = strike_increment(S)
    return math.floor(K / inc) * inc if kind == "put" else math.ceil(K / inc) * inc
