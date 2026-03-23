"""
trades.py — LBTrade 封装 + Trades 订单提交与重定价

职责：
1. LBOrderStatus：鸭子类型替代 ib_async Trade.orderStatus
2. LBTrade：鸭子类型替代 ib_async.Trade，封装 LB 订单状态与合约/订单信息
3. Trades：订单批量提交器，管理 LBTrade 列表，支持 replace_order 重定价

与 thetagang trades.py 的主要差异：
- submit_order() 改为 async（LB API 是 async）
- 重定价使用 broker.replace_order()（不取消+重下）
- 状态通过 LB WebSocket 回调（broker 初始化时注册）实时推送更新
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from rich import box
from rich.table import Table

from longbridge_wheel import log
from longbridge_wheel.compat import Contract, LimitOrder
from longbridge_wheel.fmt import dfmt, ffmt, ifmt
from longbridge_wheel.greeks import FakeContract

if TYPE_CHECKING:
    from longbridge_wheel.broker import LongbridgeBroker
    from longbridge_wheel.db import DataStore


# ---------------------------------------------------------------------------
# LB 订单终态状态集（与 ib_async Trade.isDone() 对应）
# ---------------------------------------------------------------------------

# Longbridge SDK OrderStatus 枚举值（字符串）
_DONE_STATUSES = {
    "Filled",             # 全成
    "Rejected",           # 被拒绝
    "Canceled",           # 已撤单
    "Expired",            # 已过期
    "PartialWithdrawal",  # 部分撤单
}

_STATUS_DISPLAY: dict[str, str] = {
    "Unknown": "Unknown",
    "NotReported": "Not Reported",
    "ReplacedNotReported": "Replaced NR",
    "ProtectedNotReported": "Protected NR",
    "VarietiesNotReported": "Varieties NR",
    "Filled": "Filled",
    "WaitToNew": "Wait New",
    "New": "New",
    "WaitToReplace": "Wait Modify",
    "PendingReplace": "Modifying",
    "Replaced": "Modified",
    "PartialFilled": "Partial",
    "WaitToCancel": "Wait Cancel",
    "PendingCancel": "Canceling",
    "Rejected": "Rejected",
    "Canceled": "Canceled",
    "Expired": "Expired",
    "PartialWithdrawal": "Partial Cancel",
}


# ---------------------------------------------------------------------------
# LBOrderStatus — 鸭子类型替代 ib_async.Trade.orderStatus
# ---------------------------------------------------------------------------

@dataclass
class LBOrderStatus:
    """
    鸭子类型替代 ib_async 的 OrderStatus。

    portfolio_manager 通过 trade.orderStatus.status 获取状态字符串。
    .filled 字段供 print_summary 显示成交量。
    """
    status: str = "Unknown"        # Longbridge OrderStatus 枚举值（字符串）
    filled: float = 0.0            # 已成交数量
    remaining: float = 0.0         # 剩余数量
    avgFillPrice: float = 0.0      # 平均成交价

    @property
    def display_status(self) -> str:
        """返回人类可读的状态字符串"""
        return _STATUS_DISPLAY.get(self.status, self.status)


# ---------------------------------------------------------------------------
# LBTrade — 鸭子类型替代 ib_async.Trade
# ---------------------------------------------------------------------------

@dataclass
class LBTrade:
    """
    鸭子类型替代 ib_async.Trade。

    portfolio_manager 通过以下接口访问订单状态：
        - trade.isDone()                    → 订单是否完结
        - trade.contract                    → FakeContract 对象
        - trade.order                       → LimitOrder 对象（含 action / lmtPrice / totalQuantity）
        - trade.orderStatus.status          → 状态字符串
        - trade.orderStatus.filled          → 已成交数量
        - trade.order_id                    → LB 订单号（str）

    WebSocket 回调（broker.set_on_order_changed）在 broker 侧更新 orderStatus。
    """
    order_id: str                          # LB 订单号（提交后由 LB 分配）
    contract: FakeContract                 # 合约对象（FakeOption / FakeStock）
    order: LimitOrder                      # 订单参数（action / totalQuantity / lmtPrice）
    orderStatus: LBOrderStatus = field(default_factory=LBOrderStatus)

    def isDone(self) -> bool:
        """订单是否已完结（全成 / 拒绝 / 撤单 / 过期）"""
        return self.orderStatus.status in _DONE_STATUSES

    def __repr__(self) -> str:
        return (
            f"LBTrade(order_id={self.order_id}, "
            f"symbol={self.contract.symbol}, "
            f"action={self.order.action}, "
            f"qty={self.order.totalQuantity}, "
            f"price={self.order.lmtPrice}, "
            f"status={self.orderStatus.status})"
        )


# ---------------------------------------------------------------------------
# Trades — 批量订单提交管理器
# ---------------------------------------------------------------------------

class Trades:
    """
    管理一次 manage() 周期内所有订单提交记录。

    与 thetagang 的主要差异：
    - submit_order() 是 async（LB API 调用）
    - 重定价通过 broker.replace_order()（保留 order_id，不取消+重建）
    """

    def __init__(
        self,
        broker: "LongbridgeBroker",
        data_store: Optional["DataStore"] = None,
    ) -> None:
        self.broker = broker
        self.data_store = data_store
        self.__records: List[Optional[LBTrade]] = []

    async def submit_order(
        self,
        contract: Contract,
        order: LimitOrder,
        idx: Optional[int] = None,
        intent_id: Optional[int] = None,
    ) -> None:
        """
        提交新订单，或通过 replace_order 修改已有订单价格。

        参数：
            contract    : FakeContract（股票或期权）
            order       : LimitOrder（含 action / totalQuantity / lmtPrice）
            idx         : 若非 None，修改 self.__records[idx] 的价格（重定价）
            intent_id   : 数据库 OrderIntent 的 ID，用于追踪
        """
        try:
            if idx is not None:
                # ----------------------------------------------------------------
                # 重定价路径：使用 replace_order（不取消+重建，避免空窗期）
                # ----------------------------------------------------------------
                existing = self.__records[idx]
                if existing is None or existing.isDone():
                    log.warning(
                        f"{contract.symbol}: Skipping reprice — order already done or missing"
                    )
                    return

                await self.broker.replace_order(
                    order_id=existing.order_id,
                    quantity=Decimal(str(int(order.totalQuantity))),
                    price=Decimal(str(abs(order.lmtPrice))),
                )
                # 更新本地订单记录中的价格（order_id 不变）
                existing.order = order
                log.info(
                    f"{contract.symbol}: Order repriced via replace_order, "
                    f"order_id={existing.order_id}, new_price={dfmt(order.lmtPrice)}"
                )

            else:
                # ----------------------------------------------------------------
                # 新订单路径
                # ----------------------------------------------------------------
                trade = await self.broker.place_order(contract, order)
                if self.data_store:
                    self.data_store.record_order(contract, order, intent_id=intent_id)
                self.__add_trade(trade)

        except Exception as exc:
            log.error(
                f"{contract.symbol}: Failed to submit order "
                f"action={order.action} qty={order.totalQuantity} "
                f"price={order.lmtPrice}: {exc}"
            )

    def records(self) -> List[Optional[LBTrade]]:
        """返回所有订单记录（含 None 占位符）"""
        return self.__records

    def is_empty(self) -> bool:
        """是否没有任何订单记录"""
        return len(self.__records) == 0

    def print_summary(self) -> None:
        """打印本轮提交的所有订单摘要表格"""
        if not self.__records:
            return

        table = Table(
            title="Trade Summary", show_lines=True, box=box.MINIMAL_HEAVY_HEAD
        )
        table.add_column("Symbol")
        table.add_column("Type")
        table.add_column("Action")
        table.add_column("Strike / Price")
        table.add_column("Qty")
        table.add_column("Limit Price")
        table.add_column("Order ID")
        table.add_column("Status")
        table.add_column("Filled")

        for trade in self.__records:
            if trade is None:
                continue
            contract = trade.contract
            table.add_row(
                contract.symbol,
                contract.secType,
                trade.order.action,
                dfmt(contract.strike) if contract.secType == "OPT" else "",
                ifmt(int(trade.order.totalQuantity)),
                dfmt(float(trade.order.lmtPrice)),
                trade.order_id,
                trade.orderStatus.display_status,
                ffmt(trade.orderStatus.filled, 0),
            )

        log.print(table)

    def __add_trade(self, trade: LBTrade) -> None:
        self.__records.append(trade)

    def __replace_trade(self, trade: LBTrade, idx: int) -> None:
        self.__records[idx] = trade
