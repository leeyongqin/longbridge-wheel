# longbridge-wheel

基于 [Longbridge（长桥证券）](https://longbridgeapp.com) OpenAPI 的期权 Wheel 策略自动交易系统。

从 [thetagang](https://github.com/brndnmtthws/thetagang) 移植 —— 所有策略逻辑保持不变，仅替换底层 broker 层（IBKR → Longbridge）。

## 项目概览

- **策略**：Wheel（卖 CSP put → 被行权 → 卖 covered call → 循环）
- **Broker**：Longbridge OpenAPI（美股股票 + 期权，`.US` 符号）
- **市场**：仅美股期权（Nasdaq/NYSE 上市股票）
- **配置**：`thetagang.toml`（TOML 格式，Pydantic 验证）
- **数据库**：SQLite via SQLAlchemy + Alembic 迁移

## 环境要求

- Python 3.11+
- Longbridge 账户，开通美股期权交易权限
- Nasdaq Basic 行情订阅（用于市场数据）
- Longbridge SDK v3.0.3+（支持 `calc_indexes` 返回 delta greeks）

## 认证配置

运行前设置环境变量：

```bash
export LONGBRIDGE_APP_KEY="your_app_key"
export LONGBRIDGE_APP_SECRET="your_app_secret"
export LONGBRIDGE_ACCESS_TOKEN="your_access_token"
```

在 [Longbridge OpenAPI 开发者平台](https://open.longbridgeapp.com) 获取凭证。

**模拟盘（Paper Trading）**：使用相同的 App Key/Secret，替换为模拟账户的 Access Token，并在 `[runtime.account]` 中填写模拟账户号。

## 安装

```bash
cd longbridge-wheel
pip install -e .
# 或使用 uv：
uv sync
```

## 运行

```bash
# 演习模式（不实际下单，只打印计划操作）
longbridge-wheel --config thetagang.toml --dry-run

# 实盘交易
longbridge-wheel --config thetagang.toml

# 详细日志
longbridge-wheel --config thetagang.toml --verbosity DEBUG

# 验证期权合约代码格式（首次配置时使用）
longbridge-wheel --config thetagang.toml --verify-symbols
```

## 项目架构

```
longbridge_wheel/
├── broker.py              # LongbridgeBroker —— Longbridge API 抽象层（替代 IBKR）
├── greeks.py              # FakeContract / FakeTicker / calc_indexes() 封装 + B-S fallback
├── longbridge_wheel.py    # 启动流程：加载配置、初始化数据库、连接 Broker
├── config.py              # 配置加载 + Pydantic 验证
├── config_models.py       # 所有配置段的 Pydantic 模型
├── portfolio_manager.py   # 主交易循环编排器
├── trading_operations.py  # OrderOperations + OptionChainScanner（LB 批量扫描）
├── trades.py              # LBTrade 封装 + WebSocket 订单状态 + replace_order 重定价
├── orders.py              # 内存订单队列（未变）
├── db.py                  # SQLAlchemy 模型（Run/Event/Order/Position 审计链）
├── util.py                # 持仓分析、定价工具函数（未变）
├── options.py             # DTE 计算、期权日期解析（未变）
├── exchange_hours.py      # 市场开市状态检查（未变）
├── fmt.py                 # Rich 终端格式化（未变）
├── log.py                 # 日志工具（未变）
└── strategies/            # 所有策略引擎（从 thetagang 原样复制，未变）
    ├── options_engine.py  # Put/Call 写入、Roll、Close 逻辑
    ├── equity_engine.py   # 股票买/卖再平衡
    ├── regime_engine.py   # 趋势判断 Regime 再平衡
    ├── post_engine.py     # 现金管理（VIX 对冲 v1 未启用）
    └── runtime_services.py # 依赖注入适配器
```

## 关键文件导航

| 任务 | 相关文件 |
|------|---------|
| 修改 Broker API 调用 | `broker.py` |
| 调试 greeks / delta 问题 | `greeks.py` |
| 修改订单定价逻辑 | `trades.py`, `portfolio_manager.py` |
| 修改期权链扫描逻辑 | `trading_operations.py` |
| 修改配置模式 | `config.py`, `config_models.py` |
| 添加新策略 | `strategies/options_engine.py`, `portfolio_manager.py` |

## 配置说明（`thetagang.toml`）

关键配置段：

```toml
[meta]
schema_version = 2

[run]
strategies = ["wheel", "cash_management"]
# vix_call_hedge 暂不支持（v1），已从 strategies 中移除

[runtime.account]
number = "YOUR_ACCOUNT_NUMBER"  # 实盘或模拟账户号
margin_usage = 0.5               # 使用净值的 50% 作为购买力

[runtime.longbridge]
# 认证通过环境变量传入（见上方"认证配置"）
risk_free_rate = 0.045           # 仅用于 Black-Scholes fallback（calc_indexes 返回 null 时）

[runtime.option_chains]
expirations = 4   # 扫描最近 4 个到期日
strikes = 15      # 每个到期日扫描最近 15 个 strike

[strategies.wheel.defaults.target]
dte = 45                  # 目标到期天数
delta = 0.30              # 目标 delta（卖 0.3 delta 的 put/call）
minimum_open_interest = 100  # 过滤持仓量低于 100 的合约

[strategies.wheel.defaults.roll_when]
dte = 21   # DTE <= 21 时 roll
pnl = 0.5  # P&L >= 50% 时 roll

[portfolio.symbols.SPY]
weight = 1.0   # 100% 资金分配给 SPY
```

## Broker 层说明（`broker.py`）

`LongbridgeBroker` 实现与 thetagang `IBKR` 类相同的接口：

| IBKR 方法 | Longbridge 等价 | 说明 |
|-----------|----------------|------|
| `account_summary()` | `trade_ctx.account_balance(currency="USD")` | 映射账户余额字段 |
| `portfolio()` | `trade_ctx.stock_positions()` | 解析持仓，支持股票和期权 |
| `get_chains_for_contract()` | `option_chain_expiry_date_list()` + `option_chain_info_by_date()` | 组装期权链 |
| `get_ticker_for_contract()` | `calc_indexes()` + `depth()` | 构建 FakeTicker（含 greeks + bid/ask） |
| `place_order()` | `trade_ctx.submit_order()` | 返回 LBTrade 封装 |
| `request_historical_data()` | `quote_ctx.history_candlesticks_by_date()` | 历史 K 线数据 |

## Greeks 说明（`greeks.py`）

1. **主路径**：调用 `QuoteContext.calc_indexes([symbol], [CalcIndex.Delta, ...])` 直接获取 delta（Longbridge SDK v3.0.3+ 已修复）
2. **Fallback**：若 delta 为 null（流动性差的合约），使用 Black-Scholes 公式从 IV 计算

`FakeTicker` 是适配层，将 Longbridge 数据包装成 ib_async 兼容的接口，策略引擎无需修改。

## 期权合约代码格式

| 类型 | 格式 | 示例 |
|------|------|------|
| 美股股票 | `{TICKER}.US` | `AAPL.US` |
| 美股期权 | OCC 格式（待验证） | `AAPL240119C00150000` |

期权 symbol 直接从 LB API（`option_chain_info_by_date()`）获取，无需手动构造。使用 `--verify-symbols` 在首次运行时确认格式。

## API 速率限制

| API 类型 | 限制 | 应对策略 |
|---------|------|---------|
| 行情（Quote） | 10 req/s，5 并发 | 顺序查询 + 批量 `calc_indexes()` |
| 交易（Trade） | 30 calls/30s，最小间隔 0.02s | `asyncio.sleep(0.02)` + `replace_order()` |
| WebSocket | 1 连接，500 标的 | `set_on_order_changed()` 实时推送订单状态 |

## 开发注意事项

- `strategies/` 目录下所有文件从 thetagang 原样复制，**不要修改**（除非修复 bug）
- `util.py`、`options.py`、`fmt.py`、`log.py`、`exchange_hours.py` 同样未修改
- 调试订单问题：优先查 `broker.py` 和 `trades.py`
- 数据库 schema 与 thetagang 相同，迁移文件可直接复用
- VIX call hedge 代码在 `strategies/post_engine.py` 中保留，但 v1 通过 config 禁用

## Cron 定时任务示例

```bash
# 每个交易日 10:00 ET 执行一次
0 10 * * 1-5 cd /path/to/longbridge-wheel && \
    LONGBRIDGE_APP_KEY=xxx LONGBRIDGE_APP_SECRET=yyy LONGBRIDGE_ACCESS_TOKEN=zzz \
    longbridge-wheel --config thetagang.toml >> logs/trading.log 2>&1
```
