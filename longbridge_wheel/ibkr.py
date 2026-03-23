"""
ibkr.py — ib_async IBKR 兼容适配层

从 thetagang 移植的策略文件通过 `from longbridge_wheel.ibkr import IBKR` 引用 broker。
此模块将 LongbridgeBroker 以 IBKR 别名导出，并定义策略引擎所需的辅助类型。

设计：
- IBKR = LongbridgeBroker（别名，策略文件无需修改）
- TickerField：标记需要哪些 ticker 字段（LB 中全部字段同时返回，enum 仅作标记）
- RequiredFieldValidationError：必需字段为 NaN 时抛出（与 thetagang 接口相同）
"""

from __future__ import annotations

from enum import Enum, auto


# ---------------------------------------------------------------------------
# TickerField — 标记 get_ticker_for_contract() 中必需/可选字段
# ---------------------------------------------------------------------------

class TickerField(Enum):
    """
    标记 get_ticker_for_contract() 需要返回哪些字段。

    LB 的 calc_indexes() 批量返回所有字段，此 enum 仅供策略引擎
    声明"期望存在的字段"，broker 据此决定是否验证。
    """
    MIDPOINT = auto()       # bid/ask 中间价（或 last_done fallback）
    MARKET_PRICE = auto()   # 最新成交价


# ---------------------------------------------------------------------------
# RequiredFieldValidationError — 必需字段缺失时的异常
# ---------------------------------------------------------------------------

class RequiredFieldValidationError(Exception):
    """
    当 get_ticker_for_contract() 的 required_fields 中的字段为 NaN 时抛出。

    策略引擎通过 try/except 捕获此异常，跳过无效合约。
    """
    pass


# ---------------------------------------------------------------------------
# IBKR = LongbridgeBroker（延迟导入避免循环引用）
# ---------------------------------------------------------------------------

def __getattr__(name: str):
    """
    延迟导入 LongbridgeBroker as IBKR。

    当 strategy 文件执行 `from longbridge_wheel.ibkr import IBKR` 时触发，
    避免在模块加载时引入 broker.py 的依赖（broker 依赖 longbridge SDK）。
    """
    if name == "IBKR":
        from longbridge_wheel.broker import LongbridgeBroker
        return LongbridgeBroker
    raise AttributeError(f"module 'longbridge_wheel.ibkr' has no attribute {name!r}")
