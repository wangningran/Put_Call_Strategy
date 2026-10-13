# options-wheel：Sell Put / Covered Call 轮子策略工具

三个功能加一本交易日志，配合 moomoo **模拟盘**练习用：

| 命令 | 作用 |
|---|---|
| `screen` | 筛选符合规则的 Cash-Secured Put；如果你持有正股，也筛选 Covered Call |
| `monitor` | 检查持仓：赚到 50% 提醒平仓、剩 21 天提醒处理、价内或临近行权价、财报、仓位超限 |
| `backtest` | 用历史价格回测轮子策略，并和买入持有对比 |
| `log` / `report` | 交易日志：记录每笔开仓、平仓、到期、被行权，统计盈亏和胜率 |

> 本工具只做筛选、提醒和记录，**不会自动下单**。所有数字仅供学习参考，不构成投资建议。

## 安装（在你自己的电脑上）

需要 Python 3.11 或更高版本。

```bash
cd options-wheel
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                                   # 应该全部通过
```

然后打开 `config.toml`，把 `fee_per_contract` 改成你在 moomoo App 里查到的实际费用，再看一遍 `watchlist`。

## 每天的流程（模拟盘阶段）

```bash
# 1. 开盘后筛选候选
python -m wheel screen

# 2. 在 moomoo 模拟盘下单（限价单，挂在中间价附近），成交后记日志：
python -m wheel log open SOFI put 20 2026-11-20 --premium 0.55 --notes "IV 高，财报后"

# 3. 每天收盘前检查持仓
python -m wheel monitor

# 4. 按提醒操作后，把结果记下来
python -m wheel log close 1 --price 0.27     # 买回平仓（每股价格）
python -m wheel log expire 1                 # 到期作废
python -m wheel log assign 1                 # 被行权（Put 会自动记一笔买入 100 股）

# 5. 每周或每月复盘
python -m wheel report
```

被行权拿到股票以后，`screen` 会自动列出 Covered Call 候选（默认行权价不低于你的持股成本）。日志会拒绝记录没有正股覆盖的 Call，避免误成裸卖。

日志保存在 `data/journal.csv` 和 `data/stock_ledger.csv`，可以直接用 Excel 打开。`data/` 已加入 `.gitignore`，不会被提交到仓库。

## 回测

```bash
python -m wheel backtest SOFI --start 2019-01-01 --plot sofi.png
python -m wheel backtest SPY --cash 60000 --delta 0.20          # SPY 一张约需 6 万美元
python -m wheel backtest --csv my_prices.csv                    # 用自己导出的价格（Date, Close 两列）
python -m wheel backtest --synthetic 1500 --seed 3              # 离线演示，用模拟价格
```

**回测的局限，一定要看**：免费数据里没有历史期权价格，所以期权价格是用 Black-Scholes 模型估算的，IV 取过去 20 天的实际波动率 × 1.1。这样会忽略：
- 虚值 Put 的 IV 通常更高（偏斜），实际收到的权利金可能比模型多，但风险也更大；
- 财报和暴跌前 IV 会飙升，跳空亏损比模型里更大；
- 真实的买卖价差和成交不了的情况。

所以回测只用来比较不同规则（delta、天数、止盈比例），**不能当成收益预测**。

从模拟价格的演示里已经能看到两个典型现象：
- 股价大涨时，轮子策略明显跑输买入持有（正股被叫走，上涨被封顶）；
- 股价长期下跌时，你会被套在正股里。因为 Call 只卖在成本价以上，权利金少到几乎卖不出去，期权腿"胜率 100%"，账户却在亏钱。

## 推送提醒（可选）

1. 在 Telegram 里找 @BotFather 创建机器人，拿到 token；给机器人发一条消息，然后通过 `https://api.telegram.org/bot<token>/getUpdates` 查到你的 chat_id。
2. 设置环境变量后运行：

```bash
export TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy
python -m wheel monitor --notify
```

3. 用定时任务每个交易日自动跑（macOS / Linux 用 `crontab -e`；美东 15:30 约等于北京时间 03:30 或 04:30，看夏令时）：

```
30 15 * * 1-5  cd /path/to/options-wheel && .venv/bin/python -m wheel monitor --notify
```

## 数据来源

默认使用 Yahoo Finance（yfinance），免费，大约延迟 15 分钟，盘后的 bid/ask 经常是 0，所以**筛选请在美股交易时段运行**。筛选器会根据中间价重新计算 IV 和 Delta，不直接依赖 Yahoo 给的 IV。

`wheel/data.py` 里的 `YahooProvider` 可以替换。下一步计划加 moomoo OpenD 数据源：实时报价和希腊值，并能直接读取模拟盘或实盘的持仓。

## 代码结构

```
wheel/
  bs.py        Black-Scholes 定价、Delta、隐含波动率、按 Delta 反推行权价
  config.py    读取 config.toml
  data.py      行情数据源（Yahoo）
  journal.py   交易日志和持仓、盈亏计算
  screener.py  筛选器
  monitor.py   持仓监控和提醒
  backtest.py  回测
  cli.py       命令行
tests/         单元测试（用模拟数据，不需要联网）
```
