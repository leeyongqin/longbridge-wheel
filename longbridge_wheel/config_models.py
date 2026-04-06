"""
config_models.py — 配置 Pydantic 模型定义

从 thetagang 移植，主要变更：
- 删除 IBCConfig（IBC 是 IBKR 专属守护进程）
- 删除 WatchdogConfig（IBKR 专属连接监控）
- 删除 IBAsyncConfig（ib_async 专属）
- 新增 LongbridgeConfig（Longbridge API 认证与参数）
- AccountConfig 删除 market_data_type（LB 始终返回实时行情）
- OrdersConfig 删除 algo 配置（LB 不支持 IBKR Adaptive 算法订单）
"""

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator
from rich.console import Console
from rich.table import Table
from typing_extensions import Self

from longbridge_wheel.fmt import dfmt, ffmt, pfmt

error_console = Console(stderr=True, style="bold red")


class DisplayMixin:
    def add_to_table(self, table: Table, section: str = "") -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 账户配置
# ---------------------------------------------------------------------------

class AccountConfig(BaseModel, DisplayMixin):
    # LB 认证通过 Access Token 绑定账户，number 仅用于 DB 记录标识（可留空）
    number: str = Field(default="")
    margin_usage: float = Field(..., ge=0.0)
    cancel_orders: bool = Field(default=True)
    # 注意：删除了 market_data_type（IBKR 专属），LB 始终提供实时行情

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_row("[spring_green1]Account details")
        table.add_row("", "Account number", "=", self.number or "(from Access Token)")
        table.add_row("", "Cancel existing orders", "=", f"{self.cancel_orders}")
        table.add_row(
            "",
            "Margin usage",
            "=",
            f"{self.margin_usage} ({pfmt(self.margin_usage, 0)})",
        )


# ---------------------------------------------------------------------------
# Longbridge API 配置（新增）
# ---------------------------------------------------------------------------

class LongbridgeConfig(BaseModel):
    """
    Longbridge OpenAPI 认证与运行参数配置。

    认证优先级：环境变量 > 配置文件
        环境变量：LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / LONGBRIDGE_ACCESS_TOKEN

    推荐：认证信息通过环境变量传入，不写入配置文件（防止泄露）。
    """
    app_key: str = Field(default="")         # Longbridge App Key（建议用环境变量）
    app_secret: str = Field(default="")      # Longbridge App Secret（建议用环境变量）
    access_token: str = Field(default="")    # Access Token（区分实盘/模拟盘）

    # Black-Scholes fallback 参数
    risk_free_rate: float = Field(
        default=0.045, ge=0.0, le=1.0,
        description="无风险利率，仅在 calc_indexes 返回 null delta 时用于 B-S 计算"
    )

    # 行情 API 请求间隔（秒），防止超过速率限制（10 req/s）
    quote_request_interval: float = Field(
        default=0.1, ge=0.0,
        description="行情 API 请求最小间隔（秒），默认 0.1s = 10 req/s"
    )

    # 交易 API 请求间隔（秒），LB 限制：30 calls/30s，最小间隔 0.02s
    trade_request_interval: float = Field(
        default=0.02, ge=0.02,
        description="交易 API 请求最小间隔（秒），LB 要求最小 0.02s"
    )

    # Finnhub API Key（无 USOption 行情订阅时用于获取实时期权价格）
    # 免费注册：https://finnhub.io/register
    # 也可通过环境变量 FINNHUB_API_KEY 传入
    finnhub_api_key: Optional[str] = Field(
        default=None,
        description="Finnhub API Key，无 USOption 订阅时用于获取实时期权 bid/ask/IV"
    )


# ---------------------------------------------------------------------------
# 订单配置（移除 IBKR algo 相关）
# ---------------------------------------------------------------------------

class AlgoConfig(BaseModel):
    """IBKR algo 存根 — LB 不支持，strategy/params 字段保留供 post_engine.py 兼容访问。"""
    strategy: str = Field(default="")
    params: List[Any] = Field(default_factory=list)


class OrdersConfig(BaseModel, DisplayMixin):
    minimum_credit: float = Field(default=0.0, ge=0.0)
    exchange: str = Field(default="SMART")  # 保留兼容性，LB 自动路由
    price_update_delay: List[int] = Field(
        default_factory=lambda: [30, 60], min_length=2, max_length=2
    )
    # algo 字段保留兼容性（LB 不使用，strategy/params 均为空）
    algo: AlgoConfig = Field(default_factory=AlgoConfig)

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_section()
        table.add_row("[spring_green1]Order settings")
        table.add_row("", "Exchange", "=", self.exchange)
        table.add_row("", "Price update delay", "=", f"{self.price_update_delay}")
        table.add_row("", "Minimum credit", "=", f"{dfmt(self.minimum_credit)}")


# ---------------------------------------------------------------------------
# 其余配置（从 thetagang 原样保留）
# ---------------------------------------------------------------------------

class ConstantsConfig(BaseModel, DisplayMixin):
    class WriteThreshold(BaseModel):
        write_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
        write_threshold_sigma: Optional[float] = Field(default=None, ge=0.0)

    write_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    write_threshold_sigma: Optional[float] = Field(default=None, ge=0.0)
    daily_stddev_window: str = Field(default="30 D")
    calls: Optional["ConstantsConfig.WriteThreshold"] = None
    puts: Optional["ConstantsConfig.WriteThreshold"] = None

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_section()
        table.add_row("[spring_green1]Constants")
        table.add_row("", "Daily stddev window", "=", self.daily_stddev_window)

        c_write_thresh = (
            f"{ffmt(self.calls.write_threshold_sigma)}σ"
            if self.calls and self.calls.write_threshold_sigma
            else pfmt(self.calls.write_threshold if self.calls else None)
        )
        p_write_thresh = (
            f"{ffmt(self.puts.write_threshold_sigma)}σ"
            if self.puts and self.puts.write_threshold_sigma
            else pfmt(self.puts.write_threshold if self.puts else None)
        )

        table.add_row("", "Write threshold for puts", "=", p_write_thresh)
        table.add_row("", "Write threshold for calls", "=", c_write_thresh)


class OptionChainsConfig(BaseModel):
    expirations: int = Field(..., ge=1)
    strikes: int = Field(..., ge=1)


class DatabaseConfig(BaseModel, DisplayMixin):
    enabled: bool = Field(default=True)
    path: str = Field(default="data/longbridge_wheel.db")
    url: Optional[str] = None

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_section()
        table.add_row("[spring_green1]Database")
        table.add_row("", "Enabled", "=", f"{self.enabled}")
        table.add_row("", "Path", "=", self.path)
        if self.url:
            table.add_row("", "URL", "=", self.url)

    def resolve_url(self, config_path: str) -> str:
        if self.url:
            return self.url
        base_dir = Path(config_path).resolve().parent
        db_path = Path(self.path)
        if not db_path.is_absolute():
            db_path = base_dir / db_path
        return f"sqlite:///{db_path}"


class CashManagementConfig(BaseModel, DisplayMixin):
    class Orders(BaseModel):
        exchange: str = Field(default="SMART")
        # algo 字段保留兼容性（LB 不使用）
        algo: "AlgoConfig" = Field(default_factory=lambda: AlgoConfig())

    enabled: bool = Field(default=False)
    cash_fund: str = Field(default="SGOV")
    target_cash_balance: int = Field(default=0, ge=0)
    buy_threshold: int = Field(default=10000, ge=0)
    sell_threshold: int = Field(default=10000, ge=0)
    primary_exchange: str = Field(default="")
    orders: "CashManagementConfig.Orders" = Field(
        default_factory=lambda: CashManagementConfig.Orders()
    )

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_section()
        table.add_row("[spring_green1]Cash management")
        table.add_row("", "Enabled", "=", f"{self.enabled}")
        table.add_row("", "Cash fund", "=", f"{self.cash_fund}")
        table.add_row("", "Target cash", "=", f"{dfmt(self.target_cash_balance)}")
        table.add_row("", "Buy threshold", "=", f"{dfmt(self.buy_threshold)}")
        table.add_row("", "Sell threshold", "=", f"{dfmt(self.sell_threshold)}")


class VIXCallHedgeConfig(BaseModel, DisplayMixin):
    """
    VIX call hedge 配置。

    v1 版本未实现 VIX 期权交易，此配置保留以兼容 thetagang config 格式，
    但必须在 [run] strategies 中不包含 "vix_call_hedge"。
    """
    class Allocation(BaseModel):
        weight: float = Field(..., ge=0.0)
        lower_bound: Optional[float] = Field(default=None, ge=0.0)
        upper_bound: Optional[float] = Field(default=None, ge=0.0)

    enabled: bool = Field(default=False)
    delta: float = Field(default=0.3, ge=0.0, le=1.0)
    target_dte: int = Field(default=30, gt=0)
    ignore_dte: int = Field(default=0, ge=0)
    max_dte: Optional[int] = Field(default=None, ge=1)
    close_hedges_when_vix_exceeds: Optional[float] = None
    allocation: List["VIXCallHedgeConfig.Allocation"] = Field(
        default_factory=lambda: [
            VIXCallHedgeConfig.Allocation(
                lower_bound=None, upper_bound=15.0, weight=0.0
            ),
            VIXCallHedgeConfig.Allocation(
                lower_bound=15.0, upper_bound=30.0, weight=0.01
            ),
            VIXCallHedgeConfig.Allocation(
                lower_bound=30.0, upper_bound=50.0, weight=0.005
            ),
            VIXCallHedgeConfig.Allocation(
                lower_bound=50.0, upper_bound=None, weight=0.0
            ),
        ]
    )

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_section()
        table.add_row("[spring_green1]Hedging with VIX calls")
        table.add_row("", "Enabled", "=", f"{self.enabled}")
        table.add_row("", "Target delta", "<=", f"{self.delta}")
        table.add_row("", "Target DTE", ">=", f"{self.target_dte}")


class WriteWhenConfig(BaseModel, DisplayMixin):
    class Puts(BaseModel):
        green: bool = Field(default=False)
        red: bool = Field(default=True)

    class Calls(BaseModel):
        green: bool = Field(default=True)
        red: bool = Field(default=False)
        cap_factor: float = Field(default=1.0, ge=0.0, le=1.0)
        cap_target_floor: float = Field(default=0.0, ge=0.0, le=1.0)
        excess_only: bool = Field(default=False)
        min_threshold_percent: Optional[float] = Field(default=None, ge=0.0, le=1.0)
        min_threshold_percent_relative: Optional[float] = Field(
            default=None, ge=0.0, le=1.0
        )

    calculate_net_contracts: bool = Field(default=False)
    calls: "WriteWhenConfig.Calls" = Field(
        default_factory=lambda: WriteWhenConfig.Calls()
    )
    puts: "WriteWhenConfig.Puts" = Field(default_factory=lambda: WriteWhenConfig.Puts())

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_section()
        table.add_row("[spring_green1]When writing new contracts")
        table.add_row(
            "",
            "Calculate net contract positions",
            "=",
            f"{self.calculate_net_contracts}",
        )
        table.add_row("", "Puts, write when red", "=", f"{self.puts.red}")
        table.add_row("", "Puts, write when green", "=", f"{self.puts.green}")
        table.add_row("", "Calls, write when green", "=", f"{self.calls.green}")
        table.add_row("", "Calls, write when red", "=", f"{self.calls.red}")
        table.add_row("", "Call cap factor", "=", f"{pfmt(self.calls.cap_factor)}")
        table.add_row(
            "", "Call cap target floor", "=", f"{pfmt(self.calls.cap_target_floor)}"
        )
        table.add_row("", "Excess only", "=", f"{self.calls.excess_only}")
        if self.calls.min_threshold_percent is not None:
            table.add_row(
                "",
                "Calls min threshold %",
                "=",
                f"{pfmt(self.calls.min_threshold_percent)}",
            )
        if self.calls.min_threshold_percent_relative is not None:
            table.add_row(
                "",
                "Calls min threshold % relative",
                "=",
                f"{pfmt(self.calls.min_threshold_percent_relative)}",
            )


class RollWhenConfig(BaseModel, DisplayMixin):
    class Calls(BaseModel):
        itm: bool = Field(default=True)
        always_when_itm: bool = Field(default=False)
        credit_only: bool = Field(default=False)
        has_excess: bool = Field(default=True)
        maintain_high_water_mark: bool = Field(default=False)

    class Puts(BaseModel):
        itm: bool = Field(default=False)
        always_when_itm: bool = Field(default=False)
        credit_only: bool = Field(default=False)
        has_excess: bool = Field(default=True)

    dte: int = Field(..., ge=0)
    pnl: float = Field(default=0.0, ge=0.0, le=1.0)
    min_pnl: float = Field(default=0.0)
    close_at_pnl: float = Field(default=1.0)
    close_if_unable_to_roll: bool = Field(default=False)
    max_dte: Optional[int] = Field(default=None, ge=1)
    calls: "RollWhenConfig.Calls" = Field(
        default_factory=lambda: RollWhenConfig.Calls()
    )
    puts: "RollWhenConfig.Puts" = Field(default_factory=lambda: RollWhenConfig.Puts())

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_section()
        table.add_row("[spring_green1]Close option positions")
        table.add_row("", "When P&L", ">=", f"{pfmt(self.close_at_pnl, 0)}")
        table.add_row(
            "", "Close if unable to roll", "=", f"{self.close_if_unable_to_roll}"
        )
        table.add_section()
        table.add_row("[spring_green1]Roll options when either condition is true")
        table.add_row(
            "",
            "Days to expiry",
            "<=",
            f"{self.dte} and P&L >= {self.min_pnl} ({pfmt(self.min_pnl, 0)})",
        )
        if self.max_dte:
            table.add_row(
                "",
                "P&L",
                ">=",
                f"{self.pnl} ({pfmt(self.pnl, 0)}) and DTE <= {self.max_dte}",
            )
        else:
            table.add_row("", "P&L", ">=", f"{self.pnl} ({pfmt(self.pnl, 0)})")

        table.add_row("", "Puts: credit only", "=", f"{self.puts.credit_only}")
        table.add_row("", "Puts: roll excess", "=", f"{self.puts.has_excess}")
        table.add_row("", "Calls: credit only", "=", f"{self.calls.credit_only}")
        table.add_row("", "Calls: roll excess", "=", f"{self.calls.has_excess}")
        table.add_row(
            "",
            "Calls: maintain high water mark",
            "=",
            f"{self.calls.maintain_high_water_mark}",
        )
        table.add_section()
        table.add_row("[spring_green1]When contracts are ITM")
        table.add_row("", "Roll puts", "=", f"{self.puts.itm}")
        table.add_row("", "Roll puts always", "=", f"{self.puts.always_when_itm}")
        table.add_row("", "Roll calls", "=", f"{self.calls.itm}")
        table.add_row("", "Roll calls always", "=", f"{self.calls.always_when_itm}")


class TargetConfig(BaseModel, DisplayMixin):
    class Puts(BaseModel):
        delta: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    class Calls(BaseModel):
        delta: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    dte: int = Field(..., ge=0)
    minimum_open_interest: int = Field(..., ge=0)
    maximum_new_contracts_percent: float = Field(0.05, ge=0.0, le=1.0)
    delta: float = Field(default=0.3, ge=0.0, le=1.0)
    max_dte: Optional[int] = Field(default=None, ge=1)
    maximum_new_contracts: Optional[int] = Field(default=None, ge=1)
    calls: Optional["TargetConfig.Calls"] = None
    puts: Optional["TargetConfig.Puts"] = None

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_section()
        table.add_row("[spring_green1]Write options with targets of")
        table.add_row("", "Days to expiry", ">=", f"{self.dte}")
        if self.max_dte:
            table.add_row("", "Days to expiry", "<=", f"{self.max_dte}")
        table.add_row("", "Default delta", "<=", f"{self.delta}")
        if self.puts and self.puts.delta:
            table.add_row("", "Delta for puts", "<=", f"{self.puts.delta}")
        if self.calls and self.calls.delta:
            table.add_row("", "Delta for calls", "<=", f"{self.calls.delta}")
        table.add_row(
            "",
            "Maximum new contracts",
            "=",
            f"{pfmt(self.maximum_new_contracts_percent, 0)} of buying power",
        )
        table.add_row("", "Minimum open interest", "=", f"{self.minimum_open_interest}")


class SymbolConfig(BaseModel):
    class WriteWhen(BaseModel):
        green: Optional[bool] = None
        red: Optional[bool] = None

    class Calls(BaseModel):
        cap_factor: Optional[float] = Field(default=None, ge=0, le=1)
        cap_target_floor: Optional[float] = Field(default=None, ge=0, le=1)
        excess_only: Optional[bool] = None
        delta: Optional[float] = Field(default=None, ge=0, le=1)
        write_threshold: Optional[float] = Field(default=None, ge=0, le=1)
        write_threshold_sigma: Optional[float] = Field(default=None, gt=0)
        strike_limit: Optional[float] = Field(default=None, gt=0)
        maintain_high_water_mark: Optional[bool] = None
        write_when: Optional["SymbolConfig.WriteWhen"] = Field(
            default_factory=lambda: SymbolConfig.WriteWhen()
        )

    class Puts(BaseModel):
        delta: Optional[float] = Field(default=None, ge=0, le=1)
        write_threshold: Optional[float] = Field(default=None, ge=0, le=1)
        write_threshold_sigma: Optional[float] = Field(default=None, gt=0)
        strike_limit: Optional[float] = Field(default=None, gt=0)
        write_when: Optional["SymbolConfig.WriteWhen"] = Field(
            default_factory=lambda: SymbolConfig.WriteWhen()
        )

    weight: float = Field(..., ge=0, le=1)
    primary_exchange: str = Field(default="", min_length=0)  # 允许空字符串
    delta: Optional[float] = Field(default=None, ge=0, le=1)
    write_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    write_threshold_sigma: Optional[float] = Field(default=None, gt=0)
    max_dte: Optional[int] = Field(default=None, ge=1)
    dte: Optional[int] = Field(default=None, ge=0)
    close_if_unable_to_roll: Optional[bool] = None
    calls: Optional["SymbolConfig.Calls"] = None
    puts: Optional["SymbolConfig.Puts"] = None
    adjust_price_after_delay: bool = Field(default=False)
    no_trading: Optional[bool] = None
    buy_only_rebalancing: Optional[bool] = None
    buy_only_min_threshold_shares: Optional[int] = Field(default=None, ge=1)
    buy_only_min_threshold_amount: Optional[float] = Field(default=None, ge=0.0)
    buy_only_min_threshold_percent: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )
    buy_only_min_threshold_percent_relative: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )
    write_calls_only_min_threshold_percent: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )
    write_calls_only_min_threshold_percent_relative: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )
    sell_only_rebalancing: Optional[bool] = None
    sell_only_min_threshold_shares: Optional[int] = Field(default=None, ge=1)
    sell_only_min_threshold_amount: Optional[float] = Field(default=None, ge=0.0)
    sell_only_min_threshold_percent: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )
    sell_only_min_threshold_percent_relative: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )


class RatioGateConfig(BaseModel, DisplayMixin):
    enabled: bool = Field(default=False)
    anchor: str = Field(default="")
    drift_max: float = Field(default=1.25, ge=0.0)
    var_min: float = Field(default=0.0, ge=0.0)

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_row("", "Ratio gate enabled", "=", f"{self.enabled}")
        table.add_row("", "Ratio gate anchor", "=", self.anchor or "-")
        table.add_row("", "Ratio gate drift max", "=", f"{ffmt(self.drift_max)}")
        table.add_row("", "Ratio gate var min", "=", f"{ffmt(self.var_min)}")


class RegimeRebalanceBaseEnum(str, Enum):
    net_liq = "net_liq"
    managed_stocks = "managed_stocks"
    net_liq_ex_options = "net_liq_ex_options"


class RegimeRebalanceConfig(BaseModel, DisplayMixin):
    enabled: bool = Field(default=False)
    symbols: List[str] = Field(default_factory=list)
    lookback_days: int = Field(default=40, ge=1)
    soft_band: float = Field(default=0.10, ge=0.0, le=1.0)
    hard_band: float = Field(default=0.50, ge=0.0, le=1.0)
    hard_band_rebalance_fraction: float = Field(default=1.0, gt=0.0, le=1.0)
    cooldown_days: int = Field(default=5, ge=0)
    choppiness_min: float = Field(default=3.0, ge=0.0)
    efficiency_max: float = Field(default=0.30, ge=0.0, le=1.0)
    flow_trade_min: float = Field(default=0.025, ge=0.0, le=1.0)
    flow_trade_stop: float = Field(default=0.0125, ge=0.0, le=1.0)
    flow_imbalance_tau: float = Field(default=0.70, ge=0.0, le=1.0)
    deficit_rail_start: float = Field(default=0.06, ge=0.0, le=1.0)
    deficit_rail_stop: float = Field(default=0.03, ge=0.0, le=1.0)
    eps: float = Field(default=1e-8, gt=0.0)
    order_history_lookback_days: int = Field(default=30, ge=1)
    shares_only: bool = Field(default=False)
    weight_base: RegimeRebalanceBaseEnum = Field(
        default=RegimeRebalanceBaseEnum.net_liq_ex_options
    )
    ratio_gate: Optional[RatioGateConfig] = None

    @model_validator(mode="after")
    def validate_bands(self) -> Self:
        if self.hard_band < self.soft_band:
            raise ValueError("regime_rebalance.hard_band must be >= soft_band")
        if self.flow_trade_min < self.flow_trade_stop:
            raise ValueError(
                "regime_rebalance.flow_trade_min must be >= flow_trade_stop"
            )
        if self.deficit_rail_start < self.deficit_rail_stop:
            raise ValueError(
                "regime_rebalance.deficit_rail_start must be >= deficit_rail_stop"
            )
        if self.ratio_gate is not None:
            if not self.ratio_gate.anchor:
                raise ValueError("regime_rebalance.ratio_gate.anchor must be set")
            if self.ratio_gate.anchor not in self.symbols:
                raise ValueError(
                    "regime_rebalance.ratio_gate.anchor must be in regime_rebalance.symbols"
                )
            rest_symbols = [s for s in self.symbols if s != self.ratio_gate.anchor]
            if not rest_symbols:
                raise ValueError(
                    "regime_rebalance.ratio_gate.anchor must leave at least one non-anchor symbol"
                )
        return self

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_section()
        table.add_row("[spring_green1]Regime-aware rebalancing")
        table.add_row("", "Enabled", "=", f"{self.enabled}")
        table.add_row("", "Symbols", "=", ", ".join(self.symbols) or "-")
        table.add_row("", "Lookback days", "=", f"{self.lookback_days}")
        table.add_row("", "Soft band (relative)", "=", f"{pfmt(self.soft_band, 0)}")
        table.add_row("", "Hard band (relative)", "=", f"{pfmt(self.hard_band, 0)}")
        table.add_row("", "Cooldown days", "=", f"{self.cooldown_days}")
        table.add_row("", "Shares only", "=", f"{self.shares_only}")
        table.add_row("", "Weight base", "=", f"{self.weight_base.value}")
        if self.ratio_gate is not None:
            self.ratio_gate.add_to_table(table, section)


class ActionWhenClosedEnum(str, Enum):
    wait = "wait"
    exit = "exit"
    continue_ = "continue"


class ExchangeHoursConfig(BaseModel, DisplayMixin):
    exchange: str = Field(default="XNYS")
    action_when_closed: ActionWhenClosedEnum = Field(default=ActionWhenClosedEnum.exit)
    delay_after_open: int = Field(default=1800, ge=0)
    delay_before_close: int = Field(default=1800, ge=0)
    max_wait_until_open: int = Field(default=3600, ge=0)

    def add_to_table(self, table: Table, section: str = "") -> None:
        table.add_row("[spring_green1]Exchange hours")
        table.add_row("", "Exchange", "=", self.exchange)
        table.add_row("", "Action when closed", "=", self.action_when_closed)
        table.add_row("", "Delay after open", "=", f"{self.delay_after_open}s")
        table.add_row("", "Delay before close", "=", f"{self.delay_before_close}s")
        table.add_row("", "Max wait until open", "=", f"{self.max_wait_until_open}s")
