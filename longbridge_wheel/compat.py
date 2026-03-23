"""
compat.py — ib_async 兼容层

为从 thetagang 移植的策略引擎提供 ib_async 类型的鸭子类型替代品。

设计原则：
- Contract / Option / Stock / Index 是纯标记类（无数据），仅用于 isinstance() 检查
- FakeContract（greeks.py）继承 Contract，FakeOption/FakeStock 分别继承 Option/Stock
- AccountValue / PortfolioItem / LimitOrder 提供与 ib_async 相同的字段接口
- util.isNan() 替代 ib_async.util.isNan()
- Ticker 是纯标记类，实际使用 FakeTicker（greeks.py）

所有策略文件从 longbridge_wheel.compat 导入，而非 ib_async，
以消除对 ib_async/ib-insync 包的依赖。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, List, Optional


# ---------------------------------------------------------------------------
# 合约标记类（用于 isinstance() 检查）
# ---------------------------------------------------------------------------

class Contract:
    """
    ib_async.Contract 兼容标记基类。

    支持与 ib_async 相同的位置/关键字参数构造，供策略引擎
    直接调用 Stock(symbol, exchange) / Index("VIX", "CBOE", "USD") 等。
    FakeContract（greeks.py）是 @dataclass，有自己的 __init__，不受此影响。
    """

    def __init__(
        self,
        symbol: str = "",
        exchange: str = "",
        currency: str = "USD",
        primaryExch: str = "",
        secType: str = "",
        conId: int = 0,
        multiplier: str = "",
        localSymbol: str = "",
        right: str = "",
        strike: float = 0.0,
        lastTradeDateOrContractMonth: str = "",
        **kwargs: Any,
    ) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency
        self.primaryExch = primaryExch
        self.primaryExchange = primaryExch   # ib_async alias used by options_engine
        self.secType = secType
        self.conId = conId
        self.multiplier = multiplier
        self.localSymbol = localSymbol or (f"{symbol}.US" if symbol else "")
        self.right = right
        self.strike = strike
        self.lastTradeDateOrContractMonth = lastTradeDateOrContractMonth


class Option(Contract):
    """ib_async.contract.Option 兼容标记类。期权合约的 isinstance 哨兵。"""

    def __init__(self, symbol: str = "", exchange: str = "", currency: str = "USD",
                 **kwargs: Any) -> None:
        super().__init__(symbol=symbol, exchange=exchange, currency=currency,
                         secType="OPT", **kwargs)


class Stock(Contract):
    """ib_async.contract.Stock 兼容标记类。股票合约的 isinstance 哨兵。"""

    def __init__(self, symbol: str = "", exchange: str = "", currency: str = "USD",
                 **kwargs: Any) -> None:
        super().__init__(symbol=symbol, exchange=exchange, currency=currency,
                         secType="STK", **kwargs)


class Index(Contract):
    """ib_async.contract.Index 兼容标记类。指数的 isinstance 哨兵。"""

    def __init__(self, symbol: str = "", exchange: str = "", currency: str = "USD",
                 **kwargs: Any) -> None:
        super().__init__(symbol=symbol, exchange=exchange, currency=currency,
                         secType="IND", **kwargs)


class ComboLeg:
    """ib_async.contract.ComboLeg 兼容存根（LB 不使用 combo 合约）。"""
    pass


# ---------------------------------------------------------------------------
# 账户与持仓数据类
# ---------------------------------------------------------------------------

@dataclass
class AccountValue:
    """
    ib_async.AccountValue 兼容数据类。

    account_summary() 返回 Dict[str, AccountValue]，
    策略引擎通过 .tag / .value（字符串）访问账户字段。
    """
    tag: str = ""
    value: str = "0"           # 注意：ib_async 中 value 是字符串
    currency: str = ""
    account: str = ""


@dataclass
class PortfolioItem:
    """
    ib_async.PortfolioItem 兼容数据类。

    broker.portfolio() 返回 List[PortfolioItem]，
    策略引擎通过 .contract / .position / .averageCost 等字段访问持仓信息。
    """
    contract: Contract
    position: float
    marketPrice: float
    marketValue: float
    averageCost: float
    unrealizedPNL: float
    realizedPNL: float
    account: str


# ---------------------------------------------------------------------------
# 订单数据类
# ---------------------------------------------------------------------------

@dataclass
class Order:
    """
    ib_async.Order 兼容基类。

    would_increase_spread() 使用 .action / .lmtPrice 字段。
    """
    action: str = ""                   # "BUY" 或 "SELL"
    totalQuantity: float = 0.0         # 合约数量
    lmtPrice: float = 0.0             # 限价
    orderId: int = 0                  # IBKR orderId，LB 中不使用，保留兼容性
    orderRef: str = ""                # 订单备注（用于 LB remark 字段）
    tif: str = "DAY"                  # 有效期
    account: str = ""                 # 账户号
    transmit: bool = True             # 是否立即提交


@dataclass
class LimitOrder(Order):
    """
    ib_async.order.LimitOrder 兼容类。

    OrderOperations.create_limit_order() 创建此对象，
    Trades.submit_order() 使用它来提交/替换订单。
    """
    algoStrategy: str = ""            # IBKR 专属，LB 中不使用，保留兼容性
    algoParams: List[Any] = field(default_factory=list)  # IBKR 专属


@dataclass
class TagValue:
    """ib_async.TagValue 兼容存根（IBKR algo 参数，LB 不使用）。"""
    tag: str = ""
    value: str = ""


# ---------------------------------------------------------------------------
# 行情 Ticker 标记类
# ---------------------------------------------------------------------------

class Ticker:
    """
    ib_async.Ticker 兼容标记基类。

    实际运行时使用 FakeTicker（greeks.py），
    此标记类仅供类型注解使用。
    """
    pass


# ---------------------------------------------------------------------------
# ExecutionFilter 存根
# ---------------------------------------------------------------------------

class ExecutionFilter:
    """ib_async.ExecutionFilter 兼容存根（LB 不使用成交过滤）。"""
    pass


# ---------------------------------------------------------------------------
# util 模块替代对象
# ---------------------------------------------------------------------------

class _CompatUtil:
    """
    替代 ib_async.util 模块的工具对象。

    策略引擎通过 util.isNan(x) 检查浮点数是否为 NaN。
    """

    @staticmethod
    def isNan(value: Any) -> bool:
        """检查值是否为 NaN（与 ib_async.util.isNan 行为一致）。"""
        try:
            return math.isnan(float(value))
        except (TypeError, ValueError):
            return True


# 模块级单例，策略文件通过 `util.isNan(x)` 调用
util = _CompatUtil()
