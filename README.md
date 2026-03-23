# longbridge-wheel

基于 [Longbridge（长桥证券）](https://longbridgeapp.com) OpenAPI 的期权 Wheel 策略自动交易机器人。

从 [thetagang](https://github.com/brndnmtthws/thetagang) 移植 —— 所有策略逻辑保持不变，仅将底层 Broker 从 IBKR（Interactive Brokers）替换为 Longbridge。

---

## 目录

- [项目概述](#项目概述)
- [策略说明](#策略说明)
- [架构设计](#架构设计)
- [环境要求](#环境要求)
- [安装](#安装)
- [认证配置](#认证配置)
- [配置文件](#配置文件)
- [运行](#运行)
- [项目结构](#项目结构)
- [关键模块说明](#关键模块说明)
- [API 速率限制](#api-速率限制)
- [注意事项](#注意事项)

---

## 项目概述

| 项目 | 说明 |
|------|------|
| **策略** | Wheel（卖现金担保 Put → 被行权 → 卖 Covered Call → 循环） |
| **Broker** | Longbridge OpenAPI（美股股票 + 期权） |
| **市场** | 仅美股期权（NYSE / Nasdaq 上市标的） |
| **配置格式** | TOML（Pydantic 验证） |
| **持久化** | SQLite via SQLAlchemy + Alembic 迁移 |
| **运行模式** | Cron 定时单次执行（无守护进程） |

---

## 策略说明

Wheel 策略是一种系统性的期权卖出策略：

1. **卖 Put（CSP）**：对目标标的以目标 delta（如 0.30）卖出虚值 Put，收取权利金
2. **被行权 → 持有股票**：若 Put 到期被行权，以行权价买入股票
3. **卖 Call（Covered Call）**：对持有的股票以目标 delta 卖出 Covered Call，继续收取权利金
4. **循环**：Call 到期了结后回到步骤 1

此机器人自动执行上述循环，包括：

- 到期日管理（DTE 过滤）
- Delta 目标选择
- 自动 Roll（展期）：DTE 不足或盈利达标时自动 roll 到更远到期日
- 提前平仓（buyback）：盈利达到配置比例时提前买回
- 价格重定价：提交后等待随机延迟，再按 (原价 + 中间价) / 2 调整
- 现金管理：闲置现金投入货币市场基金（如 SGOV）

---

## 架构设计

```
CLI (Click)
  └─ longbridge_wheel.py    启动：加载配置、初始化数据库、连接 Broker
       └─ PortfolioManager.manage()
            ├─ LongbridgeBroker (broker.py)   替代 IBKR，封装 LB API
            │    ├─ AsyncQuoteContext ─────────→ 行情：期权链 / greeks / 历史数据
            │    └─ AsyncTradeContext ─────────→ 交易：账户 / 持仓 / 下单 / 撤单
            ├─ greeks.py         FakeContract / FakeTicker / Black-Scholes fallback
            ├─ OptionsStrategyEngine           Put/Call 写入、Roll、Close 逻辑
            ├─ EquityRebalanceEngine           股票买/卖再平衡
            ├─ RegimeRebalanceEngine           趋势判断 Regime 再平衡（可选）
            ├─ PostStrategyEngine              现金管理（VIX 对冲 v1 未启用）
            ├─ OrderOperations + OptionChainScanner  期权链扫描 + 限价单构建
            └─ DataStore (db.py)               SQLite 审计链：Run / Event / Order / Position
```

与 thetagang（IBKR）相比的主要变化：

| 模块 | IBKR | Longbridge |
|------|------|------------|
| 连接 | IBC 守护进程 + Watchdog | OAuth API Key，无守护进程 |
| Delta | ib_async Ticker.modelGreeks | `calc_indexes()` 批量获取（B-S fallback） |
| 合约 | `ib_async.Contract` 对象 | OCC 格式字符串（如 `AAPL240119C00150000`） |
| 算法单 | Adaptive Patient 算法 | 标准限价单（LO） |
| 订单状态 | ib_async 事件循环 | WebSocket `set_on_order_changed()` |
| 重定价 | cancel + 重新下单 | `replace_order()`（保留 order_id） |

---

## 环境要求

- Python 3.11+
- Longbridge 账户，已开通**美股期权交易权限**
- **Nasdaq Basic** 行情订阅（用于市场数据）
- Longbridge SDK `longbridge >= 1.0`（支持 `calc_indexes` 返回 greeks）

---

## 安装

```bash
git clone https://github.com/leeyongqin/longbridge-wheel.git
cd longbridge-wheel

# 使用 pip
pip install -e .

# 或使用 uv（推荐）
uv sync
```

---

## 认证配置

在 [Longbridge OpenAPI 开发者平台](https://open.longbridgeapp.com) 创建应用，获取以下凭证：

```bash
export LONGBRIDGE_APP_KEY="your_app_key"
export LONGBRIDGE_APP_SECRET="your_app_secret"
export LONGBRIDGE_ACCESS_TOKEN="your_access_token"
```

**模拟盘（Paper Trading）**：使用相同的 App Key / App Secret，将 Access Token 替换为模拟账户的 Token，并在配置文件中填写模拟账户号。无需修改代码。

---

## 配置文件

复制并编辑 `thetagang.toml`：

```toml
[meta]
schema_version = 2

[run]
strategies = ["wheel", "cash_management"]

[runtime.account]
number = "YOUR_ACCOUNT_NUMBER"   # 实盘或模拟账户号
cancel_orders = true
margin_usage = 0.5               # 使用净值的 50% 作为购买力

[runtime.longbridge]
risk_free_rate = 0.045           # 仅用于 Black-Scholes fallback

[runtime.option_chains]
expirations = 4                  # 扫描最近 4 个到期日
strikes = 15                     # 每个到期日扫描最近 15 个行权价

[strategies.wheel.defaults.target]
dte = 45                         # 目标到期天数
delta = 0.30                     # 目标 delta
minimum_open_interest = 100      # 过滤持仓量不足的合约

[strategies.wheel.defaults.roll_when]
dte = 21                         # DTE <= 21 时 roll
pnl = 0.50                       # 盈利 >= 50% 时 roll

[portfolio.symbols.SPY]
weight = 1.0
adjust_price_after_delay = true
primary_exchange = "ARCA"
```

完整配置选项见 `thetagang.toml`。

---

## 运行

```bash
# 验证期权合约代码格式（首次部署时确认 LB 返回的 symbol 格式）
longbridge-wheel --config thetagang.toml --verify-symbols

# 演习模式（不实际下单，只打印计划操作）
longbridge-wheel --config thetagang.toml --dry-run

# 实盘交易
longbridge-wheel --config thetagang.toml

# 详细日志（调试用）
longbridge-wheel --config thetagang.toml --verbosity DEBUG
```

### Cron 定时任务

每个交易日 10:00 ET（UTC-5/UTC-4）执行一次：

```cron
0 15 * * 1-5 cd /path/to/longbridge-wheel && \
    LONGBRIDGE_APP_KEY=xxx \
    LONGBRIDGE_APP_SECRET=yyy \
    LONGBRIDGE_ACCESS_TOKEN=zzz \
    longbridge-wheel --config thetagang.toml >> logs/trading.log 2>&1
```

---

## 项目结构

```
longbridge-wheel/
├── pyproject.toml                  依赖声明 + CLI 入口
├── thetagang.toml                  示例配置文件
├── CLAUDE.md                       开发者参考文档
└── longbridge_wheel/
    ├── broker.py                   LongbridgeBroker —— LB API 抽象层
    ├── greeks.py                   FakeContract / FakeTicker / B-S fallback
    ├── trades.py                   LBTrade 封装 + WebSocket 订单状态
    ├── trading_operations.py       OptionChainScanner + OrderOperations
    ├── portfolio_manager.py        主交易循环编排器
    ├── config.py                   配置加载 + Pydantic 验证
    ├── config_models.py            所有配置段的 Pydantic 模型
    ├── compat.py                   ib_async 类型兼容层（Contract / Ticker 等）
    ├── ibkr.py                     LongbridgeBroker as IBKR 别名 + 辅助类型
    ├── longbridge_wheel.py         启动编排（配置 → 数据库 → Broker → manage）
    ├── main.py                     Click CLI 命令定义
    ├── entry.py                    CLI 入口重导出
    ├── db.py                       SQLAlchemy 模型（Run / Event / Order / Position）
    ├── util.py                     持仓分析、定价工具函数
    ├── options.py                  DTE 计算、期权日期解析
    ├── orders.py                   内存订单队列
    ├── exchange_hours.py           市场开市状态检查
    ├── fmt.py                      Rich 终端格式化
    ├── log.py                      日志工具
    └── strategies/
        ├── options_engine.py       Put/Call 写入、Roll、Close 策略引擎
        ├── equity_engine.py        股票买/卖再平衡引擎
        ├── regime_engine.py        趋势判断 Regime 再平衡引擎
        ├── post_engine.py          现金管理引擎（VIX 对冲 v1 未启用）
        └── runtime_services.py     依赖注入适配器
```

---

## 关键模块说明

### `broker.py` — LongbridgeBroker

实现与 thetagang `IBKR` 类相同的接口，策略引擎无需修改。

| 方法 | LB API | 说明 |
|------|--------|------|
| `account_summary()` | `account_balance(currency="USD")` | 映射账户余额字段 |
| `portfolio()` | `stock_positions()` | 解析股票 + 期权持仓 |
| `get_chains_for_contract()` | `option_chain_expiry_date_list()` + `option_chain_info_by_date()` | 构建期权链 |
| `get_tickers_for_contracts()` | `calc_indexes(all_symbols, ...)` | **批量**获取 greeks（1 次 API 调用） |
| `place_order()` | `submit_order()` | 提交限价单，返回 LBTrade |
| `replace_order()` | `replace_order()` | 修改订单价格（不取消重建） |
| `request_historical_data()` | `history_candlesticks_by_date()` | 历史 K 线（用于 regime 引擎） |

### `greeks.py` — Delta 获取策略

1. **主路径**：`calc_indexes([symbol], [CalcIndex.Delta, ...])` 直接返回 delta
2. **Fallback**：delta 为 null（流动性差合约）时，用 Black-Scholes 从 IV 计算

### `trading_operations.py` — 批量期权链扫描

```
get_chain_expiry_dates()          # 1 次 API 调用：所有到期日
  → 过滤 DTE / max_dte / expirations 上限
get_chain_strikes_for_expiry()    # N 次 API 调用（N = expirations，通常 4）
  → 过滤 valid_strike / chain_strikes 上限，构建 FakeOption 列表
get_tickers_for_contracts()       # 1 次批量 calc_indexes()：获取所有候选合约 greeks
  → 过滤 price / delta / open_interest
  → 排序（delta 最优 → DTE 最近）→ 返回最优合约
```

---

## API 速率限制

| API 类型 | 限制 | 应对策略 |
|---------|------|---------|
| 行情（Quote） | 10 req/s，5 并发 | 批量 `calc_indexes()` + 顺序查询 |
| 交易（Trade） | 30 calls/30s，最小间隔 0.02s | 每次交易 API 调用前 `asyncio.sleep(0.02)` |
| WebSocket | 1 连接，500 标的 | `set_on_order_changed()` 实时推送，不占 REST 配额 |

---

## 注意事项

- **`strategies/` 目录**中所有文件从 thetagang 原样复制，不要修改（除非修复 bug）
- **VIX call hedge** 代码在 `post_engine.py` 中保留，但 v1 通过 config 禁用（`enabled = false`）
- **期权 symbol 格式**假设为 OCC 格式（`AAPL240119C00150000`），首次部署前请用 `--verify-symbols` 确认
- **数据库 schema** 与 thetagang 完全相同，Alembic 迁移文件可直接复用
- **`conId`** 字段在 LB 中无意义，所有 FakeContract 的 `conId = 0`

---

## 致谢

- 策略引擎和核心交易逻辑来自 [thetagang](https://github.com/brndnmtthws/thetagang)（MIT License）
- Broker 层适配为 [Longbridge OpenAPI](https://open.longbridgeapp.com)

## License

MIT
