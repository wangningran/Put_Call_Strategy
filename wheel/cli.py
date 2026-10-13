"""Command line entry point: python -m wheel <command>"""

import argparse
import sys
from datetime import date

from . import config as config_mod
from .journal import Journal


def _journal(cfg):
    return Journal(cfg.path("journal_path"), cfg.path("stock_ledger_path"))


def _provider():
    from .data import YahooProvider
    return YahooProvider()


def _uncovered(j):
    pos = j.positions()
    for ticker, p in pos.items():
        covered = sum(t.contracts * 100 for t in j.open_trades() if t.ticker == ticker and t.kind == "call")
        p["uncovered"] = max(p["shares"] - covered, 0)
    return pos


def _free_cash(cfg, j):
    committed = j.stats()["collateral"] + sum(p["shares"] * p["avg_cost"] for p in j.positions().values())
    return cfg.account_size * (1 - cfg.cash_reserve_pct) - committed


def _print_candidates(cands, top):
    if not cands:
        print("  没有符合规则的合约")
        return
    print(f"  {'代码':<6}{'类型':<5}{'现价':>8}{'行权价':>8}{'到期日':>12}{'天数':>5}{'中间价':>7}"
          f"{'Delta':>7}{'IV':>6}{'权利金':>8}{'占用资金':>9}{'收益':>7}{'年化':>7}{'安全垫':>7}")
    for c in cands[:top]:
        print(f"  {c.ticker:<6}{c.kind:<5}{c.price:>8.2f}{c.strike:>8.2f}{c.expiry.isoformat():>12}{c.dte:>5}"
              f"{c.mid:>7.2f}{c.delta:>7.2f}{c.iv:>6.0%}{c.premium:>8.0f}{c.collateral or c.price * 100:>9.0f}"
              f"{c.ret:>7.2%}{c.annualized:>7.1%}{c.cushion:>7.1%}")


def cmd_screen(args, cfg):
    from .screener import screen_calls, screen_puts
    j = _journal(cfg)
    p = _provider()
    cash = args.cash if args.cash is not None else _free_cash(cfg, j)
    print(f"可用于卖 Put 的现金：${cash:,.0f}（账户 ${cfg.account_size:,.0f}，保留 {cfg.cash_reserve_pct:.0%} 现金）")
    print(f"规则：{cfg.dte_min}-{cfg.dte_max} 天到期，|Delta| {cfg.delta_min}-{cfg.delta_max}，"
          f"未平仓量 ≥ {cfg.min_open_interest}，价差 ≤ {cfg.max_spread_pct:.0%}，"
          f"{'避开财报' if cfg.avoid_earnings else '不避开财报'}\n")
    print("== Cash-Secured Put 候选 ==")
    _print_candidates(screen_puts(p, cfg, cash), args.top)
    calls = screen_calls(p, cfg, _uncovered(j))
    if calls or any(v["uncovered"] >= 100 for v in _uncovered(j).values()):
        print("\n== Covered Call 候选（基于你持有的正股）==")
        _print_candidates(calls, args.top)
    print("\n提示：年化高通常意味着波动和风险高；只卖你愿意按行权价持有的股票。下单用限价单挂在中间价附近。")


def cmd_monitor(args, cfg):
    from .monitor import check, notify_telegram
    j = _journal(cfg)
    if not j.open_trades() and not any(p["shares"] for p in j.positions().values()):
        print("没有持仓。")
        return
    alerts, rows = check(_provider(), j, cfg)
    if rows:
        print(f"  {'#':<4}{'代码':<6}{'类型':<5}{'行权价':>8}{'到期日':>12}{'天数':>5}{'现价':>8}"
              f"{'卖出价':>7}{'现值':>7}{'已赚':>7}")
        for r in rows:
            mid = f"{r['mid']:.2f}" if r["mid"] is not None else "-"
            cap = f"{r['captured']:.0%}" if r["captured"] is not None else "-"
            print(f"  {r['id']:<4}{r['ticker']:<6}{r['kind']:<5}{r['strike']:>8.2f}{r['expiry']:>12}{r['dte']:>5}"
                  f"{r['price']:>8.2f}{r['premium']:>7.2f}{mid:>7}{cap:>7}")
    print()
    if not alerts:
        print("一切正常，无需操作。")
    for a in alerts:
        print(a)
    if args.notify and alerts:
        text = "期权持仓提醒\n" + "\n".join(str(a) for a in alerts)
        print("\n已推送到 Telegram" if notify_telegram(text) else "\n未设置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，跳过推送")


def cmd_backtest(args, cfg):
    from . import backtest as bt
    from .data import load_csv_history
    if args.csv:
        closes = load_csv_history(args.csv)
        name = args.csv
    elif args.synthetic:
        closes = bt.synthetic_prices(days=args.synthetic, seed=args.seed, start=40.0)
        name = "synthetic"
    else:
        closes = _provider().history(args.ticker, args.start, args.end)
        name = args.ticker
    if args.csv and (args.start or args.end):
        closes = closes.loc[args.start:args.end]
    params = bt.Params(
        initial_cash=args.cash or cfg.account_size, put_delta=args.delta, call_delta=args.call_delta or args.delta,
        dte=args.dte, take_profit=args.take_profit if args.take_profit is not None else cfg.take_profit,
        iv_multiplier=args.iv_mult, fee_per_contract=cfg.fee_per_contract,
        cash_reserve_pct=cfg.cash_reserve_pct, risk_free_rate=cfg.risk_free_rate,
        calls_above_cost_basis=cfg.calls_above_cost_basis,
    )
    res = bt.run(closes, params)
    m = res.metrics
    print(f"回测：{name}  {m['start']} → {m['end']}  初始资金 ${params.initial_cash:,.0f}")
    print(f"参数：Put delta {params.put_delta}，Call delta {params.call_delta}，{params.dte} 天，"
          f"止盈 {params.take_profit:.0%}，IV = 已实现波动率 × {params.iv_multiplier}\n")
    print(f"  {'':<14}{'轮子策略':>12}{'买入持有':>12}")
    print(f"  {'总收益':<14}{m['total_return']:>12.1%}{m['bh_total_return']:>12.1%}")
    print(f"  {'年化收益':<14}{m['cagr']:>12.1%}{m['bh_cagr']:>12.1%}")
    print(f"  {'最大回撤':<14}{m['max_drawdown']:>12.1%}{m['bh_max_drawdown']:>12.1%}")
    print(f"  {'最差单月':<14}{m['worst_month']:>12.1%}")
    if m["option_trades"]:
        print(f"\n  期权交易 {m['option_trades']} 笔，期权腿胜率 {m['option_win_rate']:.0%}（不含正股盈亏，别被它骗了），"
              f"Put 被行权 {m['put_assignments']} 次，正股被叫走 {m['calls_called_away']} 次")
        print(f"  净权利金 ${m['net_premium']:,.0f}，平均每月 ${m['avg_monthly_premium']:,.0f}")
    else:
        print("\n  没有成交任何期权（资金可能不够买 100 股）")
    print("\n注意：期权价格由模型估算，忽略了偏斜和事件前的 IV 飙升，实盘通常更差。")
    if args.out:
        res.equity.to_frame().assign(buy_hold=closes / closes.iloc[0] * params.initial_cash).to_csv(args.out)
        res.trades.to_csv(args.out.replace(".csv", "_trades.csv"), index=False)
        print(f"净值曲线已保存到 {args.out}")
    if args.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        ax = res.equity.plot(label="Wheel", figsize=(10, 5))
        (closes / closes.iloc[0] * params.initial_cash).plot(ax=ax, label="Buy & hold", alpha=0.7)
        ax.set_title(f"Wheel backtest: {name}")
        ax.legend()
        plt.tight_layout()
        plt.savefig(args.plot, dpi=120)
        print(f"图表已保存到 {args.plot}")


def cmd_log(args, cfg):
    j = _journal(cfg)
    on = date.fromisoformat(args.date) if getattr(args, "date", None) else None
    if args.action == "open":
        fees = args.fees if args.fees is not None else cfg.fee_per_contract * args.contracts
        t = j.open(args.ticker, args.kind, args.strike, args.expiry, args.contracts, args.premium, fees, on, args.notes)
        print(f"已记录 #{t.id}：卖出 {t.contracts} 张 {t.ticker} {t.kind} {t.strike:g} {t.expiry}，"
              f"收入 ${t.premium * 100 * t.contracts - fees:,.2f}（扣费后）")
    elif args.action == "close":
        fees = args.fees if args.fees is not None else cfg.fee_per_contract * j.get(args.id).contracts
        t = j.close(args.id, args.price, fees, on)
        print(f"#{t.id} 已平仓，盈亏 ${t.realized_pnl:,.2f}")
    elif args.action == "expire":
        t = j.expire(args.id, on)
        print(f"#{t.id} 作废到期，盈亏 ${t.realized_pnl:,.2f}")
    elif args.action == "assign":
        t = j.assign(args.id, args.fees, on)
        verb = "买入" if t.kind == "put" else "卖出"
        print(f"#{t.id} 被行权：以 ${t.strike:g} {verb} {t.contracts * 100} 股 {t.ticker}，期权盈亏 ${t.realized_pnl:,.2f}")
    elif args.action == "stock":
        f = j.stock(args.ticker, args.shares, args.price, args.fees, on)
        print(f"已记录股票成交：{f.ticker} {f.shares:+d} 股 @ ${f.price:g}")
    j.save()


def cmd_report(args, cfg):
    j = _journal(cfg)
    s = j.stats()
    print("== 交易日志汇总 ==")
    print(f"  已结束交易 {s['closed_trades']} 笔，进行中 {s['open_trades']} 笔，胜率 {s['win_rate']:.0%}，被行权 {s['assignments']} 次")
    print(f"  期权已实现盈亏 ${s['option_pnl']:,.2f}，股票已实现盈亏 ${s['stock_pnl']:,.2f}")
    print(f"  当前 Put 占用现金 ${s['collateral']:,.0f}")
    if s["monthly"]:
        print("\n  月份      期权盈亏")
        for month, pnl in s["monthly"].items():
            print(f"  {month}  {pnl:>10,.2f}")
    pos = {k: v for k, v in j.positions().items() if v["shares"]}
    if pos:
        print("\n  正股持仓：")
        for ticker, p in pos.items():
            print(f"  {ticker:<6}{p['shares']:>6} 股  均价 ${p['avg_cost']:.2f}  扣除已收权利金后成本 ${p['net_basis']:.2f}")
    if j.open_trades():
        print("\n  进行中的期权：")
        for t in j.open_trades():
            print(f"  #{t.id:<3} {t.ticker:<6}{t.kind:<5}{t.strike:>8g}  {t.expiry}  x{t.contracts}  卖出价 {t.premium:.2f}")


def build_parser():
    ap = argparse.ArgumentParser(prog="wheel", description="Sell Put / Covered Call 轮子策略工具")
    ap.add_argument("--config", help="config.toml 路径（默认使用项目根目录下的）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen", help="筛选可卖的 Put / Covered Call")
    s.add_argument("--cash", type=float, help="覆盖可用现金")
    s.add_argument("--top", type=int, default=15)
    s.set_defaults(func=cmd_screen)

    m = sub.add_parser("monitor", help="检查持仓并给出提醒")
    m.add_argument("--notify", action="store_true", help="通过 Telegram 推送")
    m.set_defaults(func=cmd_monitor)

    b = sub.add_parser("backtest", help="回测轮子策略")
    b.add_argument("ticker", nargs="?", default="SPY")
    b.add_argument("--start", default="2018-01-01")
    b.add_argument("--end")
    b.add_argument("--csv", help="用本地 CSV（Date, Close 列）代替在线数据")
    b.add_argument("--synthetic", type=int, metavar="DAYS", help="用模拟价格（离线演示）")
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--cash", type=float)
    b.add_argument("--delta", type=float, default=0.25)
    b.add_argument("--call-delta", type=float)
    b.add_argument("--dte", type=int, default=35)
    b.add_argument("--take-profit", type=float)
    b.add_argument("--iv-mult", type=float, default=1.10)
    b.add_argument("--out", help="保存净值曲线 CSV")
    b.add_argument("--plot", help="保存图表 PNG（需要 matplotlib）")
    b.set_defaults(func=cmd_backtest)

    lg = sub.add_parser("log", help="记录交易")
    lsub = lg.add_subparsers(dest="action", required=True)
    o = lsub.add_parser("open", help="卖出开仓")
    o.add_argument("ticker")
    o.add_argument("kind", choices=["put", "call"])
    o.add_argument("strike", type=float)
    o.add_argument("expiry", help="YYYY-MM-DD")
    o.add_argument("--premium", type=float, required=True, help="每股成交价，如 0.45")
    o.add_argument("--contracts", type=int, default=1)
    o.add_argument("--fees", type=float, help="默认 fee_per_contract × 张数")
    o.add_argument("--notes", default="")
    c = lsub.add_parser("close", help="买回平仓")
    c.add_argument("id", type=int)
    c.add_argument("--price", type=float, required=True, help="每股买回价")
    c.add_argument("--fees", type=float)
    e = lsub.add_parser("expire", help="到期作废")
    e.add_argument("id", type=int)
    a = lsub.add_parser("assign", help="被行权")
    a.add_argument("id", type=int)
    a.add_argument("--fees", type=float, default=0.0)
    st = lsub.add_parser("stock", help="手动记录股票买卖（卖出用负数）")
    st.add_argument("ticker")
    st.add_argument("shares", type=int)
    st.add_argument("price", type=float)
    st.add_argument("--fees", type=float, default=0.0)
    for p in (o, c, e, a, st):
        p.add_argument("--date", help="成交日期 YYYY-MM-DD，默认今天")
    lg.set_defaults(func=cmd_log)

    r = sub.add_parser("report", help="交易日志汇总")
    r.set_defaults(func=cmd_report)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = config_mod.load(args.config)
    try:
        args.func(args, cfg)
    except (ValueError, KeyError) as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
