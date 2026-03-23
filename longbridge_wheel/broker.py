"""
broker.py — LongbridgeBroker：Longbridge API 抽象层

职责：
1. 实现与 thetagang IBKR 类相同的接口，让策略引擎无需修改
2. 封装 AsyncQuoteContext（行情）和 AsyncTradeContext（交易）
3. 通过 WebSocket 回调实时更新 LBTrade 订单状态
4. 提供批量 calc_indexes() 调用以获取 greeks（delta/gamma/theta/vega）
5. 支持 replace_order() 重定价（不取消+重下）

与 IBKR 类的主要差异：
- 所有方法均为 async（LB API 全异步）
- portfolio() 是 async（LB 无内存缓存，需调用 API）
- get_chains_for_contract() 返回 FakeOptionChain 鸭子类型
- qualify_contracts() 无操作（LB symbol 已自我描述）
- cancel_order() 接受 order_id 字符串（不是 ib_async Order 对象）
- request_executions() 返回空列表（v1 不支持）

速率限制：
- 行情 API：10 req/s，5 并发
- 交易 API：30 calls/30s，最小间隔 0.02s
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from longbridge_wheel import log
from longbridge_wheel.compat import AccountValue, LimitOrder, PortfolioItem
from longbridge_wheel.greeks import (
    FakeContract,
    FakeOption,
    FakeStock,
    FakeTicker,
    build_fake_ticker,
    build_stock_contract,
    build_stock_ticker,
    decimal_to_float,
    parse_option_symbol,
)
from longbridge_wheel.trades import LBTrade, LBOrderStatus, _DONE_STATUSES

if TYPE_CHECKING:
    from longbridge_wheel.config import Config
    from longbridge_wheel.db import DataStore


# ---------------------------------------------------------------------------
# LB OrderStatus 名称提取辅助函数
# ---------------------------------------------------------------------------

_LB_STATUS_NAMES = [
    "Unknown", "NotReported", "ReplacedNotReported", "ProtectedNotReported",
    "VarietiesNotReported", "Filled", "WaitToNew", "New", "WaitToReplace",
    "PendingReplace", "Replaced", "PartialFilled", "WaitToCancel",
    "PendingCancel", "Rejected", "Canceled", "Expired", "PartialWithdrawal",
]


def _lb_status_name(status: Any) -> str:
    """
    从 LB SDK OrderStatus 对象提取状态名称字符串。

    LB SDK（Rust/PyO3 binding）的 OrderStatus 在运行时是单例实例而非子类，
    type(status).__name__ 返回 "OrderStatus" 而非 "New"。
    使用 is 身份比较来正确提取名称。
    """
    from longbridge.openapi import OrderStatus

    for name in _LB_STATUS_NAMES:
        if status is getattr(OrderStatus, name, None):
            return name
    # fallback: 尝试类名（subclass pattern）或直接字符串化
    name = type(status).__name__
    return name if name != "OrderStatus" else str(status)


# ---------------------------------------------------------------------------
# FakeOptionChain — 鸭子类型替代 ib_async.OptionChain
# ---------------------------------------------------------------------------

@dataclass
class FakeOptionChain:
    """
    鸭子类型替代 ib_async.OptionChain。

    OptionChainScanner.find_eligible_contracts() 访问：
        - chain.exchange      : 交易所（匹配 underlying.exchange）
        - chain.tradingClass  : 通常与标的代码相同
        - chain.expirations   : List[str] YYYYMMDD 格式到期日
        - chain.strikes       : List[float] 行权价列表
    """
    exchange: str
    tradingClass: str
    expirations: List[str]
    strikes: List[float]
    multiplier: str = "100"


# ---------------------------------------------------------------------------
# _ACTIVE_STATUSES — 视为"未完结"的订单状态
# ---------------------------------------------------------------------------

_ACTIVE_LB_STATUSES = {
    "Unknown", "NotReported", "ReplacedNotReported",
    "ProtectedNotReported", "VarietiesNotReported",
    "WaitToNew", "New",
    "WaitToReplace", "PendingReplace", "Replaced",
    "PartialFilled", "WaitToCancel", "PendingCancel",
}

# LB CalcIndex 索引常量（批量 greeks 所需）
_GREEKS_INDEXES: List[Any] = []  # 延迟初始化，避免在模块加载时导入 SDK


def _get_greeks_indexes() -> List[Any]:
    """延迟导入 CalcIndex，避免 SDK 在模块加载时被初始化"""
    global _GREEKS_INDEXES
    if not _GREEKS_INDEXES:
        from longbridge.openapi import CalcIndex
        _GREEKS_INDEXES = [
            CalcIndex.LastDone,
            CalcIndex.Delta,
            CalcIndex.Gamma,
            CalcIndex.Theta,
            CalcIndex.Vega,
            CalcIndex.Rho,
            CalcIndex.ImpliedVolatility,
            CalcIndex.OpenInterest,
        ]
    return _GREEKS_INDEXES


# ---------------------------------------------------------------------------
# LongbridgeBroker — 主 Broker 类
# ---------------------------------------------------------------------------

class LongbridgeBroker:
    """
    Longbridge API 抽象层，实现与 thetagang IBKR 类相同的接口。

    所有 I/O 方法均为 async，调用方需用 await 等待结果。

    用法：
        broker = LongbridgeBroker(config, data_store=ds)
        await broker.setup()    # 订阅 WebSocket，连接 LB
        ...
        await broker.teardown() # 关闭连接
    """

    def __init__(
        self,
        config: "Config",
        data_store: Optional["DataStore"] = None,
    ) -> None:
        self.config = config
        self.data_store = data_store
        self._active_trades: Dict[str, LBTrade] = {}  # order_id → LBTrade

        # 延迟创建 LB context（在 setup() 中初始化）
        self._quote_ctx: Any = None
        self._trade_ctx: Any = None

    async def setup(self) -> None:
        """
        初始化 LB 连接，订阅 WebSocket 订单推送。

        必须在调用任何其他方法前 await 此方法。
        """
        from longbridge.openapi import (
            AsyncQuoteContext,
            AsyncTradeContext,
            Config as LBConfig,
            TopicType,
        )

        lb_config = LBConfig.from_apikey_env()
        loop = asyncio.get_running_loop()
        # create() 实际返回 Future，需要 await（pyi 类型注解有误）
        self._quote_ctx = await AsyncQuoteContext.create(lb_config, loop_=loop)
        self._trade_ctx = await AsyncTradeContext.create(lb_config, loop_=loop)

        # 注册订单变化回调（WebSocket 实时推送，不占 REST 配额）
        self._trade_ctx.set_on_order_changed(self._on_order_changed)
        # 订阅私有主题（接收订单推送）
        await self._trade_ctx.subscribe([TopicType.Private])
        log.info("LongbridgeBroker: WebSocket 连接已建立，订单推送已订阅")

    async def teardown(self) -> None:
        """关闭 LB 连接（程序退出前调用）"""
        # AsyncQuoteContext/AsyncTradeContext 目前无显式 close API；
        # Python GC 会在对象销毁时释放资源
        self._quote_ctx = None
        self._trade_ctx = None
        log.info("LongbridgeBroker: 连接已关闭")

    # ------------------------------------------------------------------
    # WebSocket 回调
    # ------------------------------------------------------------------

    def _on_order_changed(self, event: Any) -> None:
        """
        LB WebSocket 推送：订单状态变化回调。

        实时更新 LBTrade.orderStatus，策略引擎通过 trade.isDone() 感知。
        """
        order_id = getattr(event, "order_id", None)
        if order_id is None:
            return

        trade = self._active_trades.get(order_id)
        if trade is None:
            return

        # 从 LB OrderStatus 枚举类型提取状态名称字符串
        status_name = _lb_status_name(event.status)

        # 不允许用非终态状态覆盖已终态的订单（防止 WS 事件乱序导致虚假"未完结"日志）
        if (
            trade.orderStatus.status in _DONE_STATUSES
            and status_name not in _DONE_STATUSES
        ):
            log.info(
                f"{trade.contract.symbol}: 忽略状态回退事件 "
                f"{trade.orderStatus.status} → {status_name} (order_id={order_id})"
            )
            return

        trade.orderStatus.status = status_name
        trade.orderStatus.filled = float(getattr(event, "executed_quantity", 0) or 0)

        submitted_qty = float(getattr(event, "submitted_quantity", 0) or 0)
        trade.orderStatus.remaining = submitted_qty - trade.orderStatus.filled

        exec_price = getattr(event, "executed_price", None)
        if exec_price is not None:
            trade.orderStatus.avgFillPrice = float(exec_price)

        if status_name == "Filled":
            log.info(
                f"{trade.contract.symbol}: 订单已全成 order_id={order_id} "
                f"price={trade.orderStatus.avgFillPrice}"
            )
        elif status_name in ("Rejected",):
            msg = getattr(event, "msg", "")
            log.warning(
                f"{trade.contract.symbol}: 订单被拒绝 order_id={order_id} msg={msg}"
            )
        elif status_name in ("Canceled", "PartialWithdrawal", "Expired"):
            log.info(
                f"{trade.contract.symbol}: 订单已结束 status={status_name} order_id={order_id}"
            )

        if self.data_store:
            try:
                self.data_store.record_order_status(trade)
            except Exception:
                pass  # 数据库记录失败不影响主流程

    def _register_trade(self, trade: LBTrade) -> None:
        """注册订单到活跃订单表，供 WebSocket 回调更新状态"""
        self._active_trades[trade.order_id] = trade

    # ------------------------------------------------------------------
    # 账户与持仓
    # ------------------------------------------------------------------

    async def account_summary(self, account: str) -> List[AccountValue]:
        """
        获取账户余额，映射为 thetagang 兼容的 AccountValue 列表。

        映射关系：
            LB net_assets         → NetLiquidation
            LB buy_power          → BuyingPower, ExcessLiquidity
            LB total_cash         → TotalCashValue
            LB init_margin        → InitMarginReq
            LB maintenance_margin → FullMaintMarginReq, MaintMarginReq
            计算 cushion          → BuyingPower / NetLiquidation
        """
        await asyncio.sleep(0.02)  # 交易 API 速率限制
        balances = await self._trade_ctx.account_balance(currency="USD")

        if not balances:
            log.warning("account_summary: 未获取到 USD 账户余额")
            return []

        usd = balances[0]
        net = float(usd.net_assets)
        bp = float(usd.buy_power)
        cash = float(usd.total_cash)
        init_margin = float(usd.init_margin)
        maint_margin = float(usd.maintenance_margin)
        cushion = bp / net if net > 0 else 0.0

        return [
            AccountValue(tag="NetLiquidation", value=str(net), account=account),
            AccountValue(tag="BuyingPower", value=str(bp), account=account),
            AccountValue(tag="ExcessLiquidity", value=str(bp), account=account),
            AccountValue(tag="TotalCashValue", value=str(cash), account=account),
            AccountValue(tag="InitMarginReq", value=str(init_margin), account=account),
            AccountValue(tag="MaintMarginReq", value=str(maint_margin), account=account),
            AccountValue(tag="FullMaintMarginReq", value=str(maint_margin), account=account),
            AccountValue(tag="Cushion", value=str(cushion), account=account),
        ]

    async def portfolio(self, account: str) -> List[PortfolioItem]:
        """
        获取账户持仓，映射为 thetagang 兼容的 PortfolioItem 列表。

        LB 的 stock_positions() 同时返回股票和期权持仓（均在同一接口下）。
        多个 channel（股票账户 / 期权账户）可能重复报告同一持仓，使用
        localSymbol 去重，保留持仓量绝对值最大的那条。
        """
        await asyncio.sleep(0.02)
        resp = await self._trade_ctx.stock_positions()

        # 使用 localSymbol 去重，避免多 channel 重复
        seen: Dict[str, PortfolioItem] = {}

        for ch in resp.channels:
            for pos in ch.positions:
                contract = self._lb_symbol_to_contract(pos.symbol)
                if contract is None:
                    log.warning(f"portfolio: 无法解析 symbol={pos.symbol}，已跳过")
                    continue

                qty = float(pos.quantity)
                if qty == 0:
                    continue

                cost = float(pos.cost_price)
                item = PortfolioItem(
                    contract=contract,
                    position=qty,
                    marketPrice=cost,    # LB 持仓无实时价格；用成本价占位
                    marketValue=qty * cost,
                    averageCost=cost,
                    unrealizedPNL=0.0,   # LB 持仓 API 不返回 PNL
                    realizedPNL=0.0,
                    account=account,
                )

                key = contract.localSymbol
                existing = seen.get(key)
                if existing is None or abs(qty) >= abs(existing.position):
                    seen[key] = item

        # 为每个持仓分配唯一 conId（portfolio_manager 用 conId 作为 position_values 的 key，
        # 所有 FakeContract.conId 默认为 0 会导致数据互相覆盖）
        result = list(seen.values())
        for i, item in enumerate(result):
            item.contract.conId = i + 1
        return result

    # ------------------------------------------------------------------
    # 合约工具
    # ------------------------------------------------------------------

    async def qualify_contracts(self, *contracts: Any) -> List[Any]:
        """
        LB 中合约已自我描述（symbol 字符串包含全部信息），无需查询。

        直接返回传入的合约列表，保持与 thetagang 接口兼容。
        """
        return list(contracts)

    def _lb_symbol_to_contract(self, lb_symbol: str) -> Optional[FakeContract]:
        """
        将 LB symbol 字符串转换为 FakeContract。

        规则：
            "AAPL.US"                  → FakeStock（股票：点前全为字母）
            "SPY260618P640000.US"      → FakeOption（期权：点前含数字）
            "AAPL240119C00150000"      → FakeOption（期权，无后缀）
        """
        if not lb_symbol:
            return None

        if "." in lb_symbol:
            pre_dot = lb_symbol.split(".")[0]
            # 股票：ticker 全为字母（如 "AAPL", "SPY", "TSLA"）
            # 期权：ticker 含数字（如 "SPY260618P640000"）
            if pre_dot.isalpha():
                return build_stock_contract(pre_dot)
            else:
                # 期权带 .US 后缀（LB 格式：如 "SPY260618P640000.US"）
                return parse_option_symbol(lb_symbol)

        # 无后缀：尝试期权解析（老格式 OCC）
        return parse_option_symbol(lb_symbol)

    # ------------------------------------------------------------------
    # 期权链
    # ------------------------------------------------------------------

    async def get_chains_for_contract(
        self, contract: FakeContract
    ) -> List[FakeOptionChain]:
        """
        获取标的的期权链，返回 FakeOptionChain 鸭子类型列表。

        流程：
        1. option_chain_expiry_date_list() → 所有可用到期日
        2. 聚合所有 standard=True 的行权价

        返回的 FakeOptionChain 兼容 OptionChainScanner.find_eligible_contracts() 中的
        chain.exchange / chain.tradingClass / chain.expirations / chain.strikes 访问。
        """
        lb_symbol = f"{contract.symbol}.US"

        expiry_dates = await self._quote_ctx.option_chain_expiry_date_list(lb_symbol)
        if not expiry_dates:
            return []

        all_expirations: List[str] = []
        all_strikes: set[float] = set()

        for expiry in expiry_dates:
            expiry_str = expiry.strftime("%Y%m%d")
            all_expirations.append(expiry_str)

            try:
                strike_infos = await self._quote_ctx.option_chain_info_by_date(
                    lb_symbol, expiry
                )
                for si in strike_infos:
                    if si.standard:
                        all_strikes.add(float(si.price))
            except Exception as exc:
                log.warning(
                    f"{contract.symbol}: 获取 {expiry_str} 链数据失败: {exc}"
                )

        chain = FakeOptionChain(
            exchange=contract.exchange,
            tradingClass=contract.symbol,
            expirations=all_expirations,
            strikes=sorted(all_strikes),
        )
        return [chain]

    async def get_chain_strikes_for_expiry(
        self,
        underlying_symbol: str,
        expiry: date,
    ) -> List[Any]:
        """
        获取特定到期日的 StrikePriceInfo 列表（含 call_symbol / put_symbol）。

        由 trading_operations.py 的 OptionChainScanner 直接调用，
        用于批量获取期权 symbol 后传入 calc_indexes()。
        """
        lb_symbol = f"{underlying_symbol}.US"
        return await self._quote_ctx.option_chain_info_by_date(lb_symbol, expiry)

    async def get_chain_expiry_dates(self, underlying_symbol: str) -> List[date]:
        """
        获取标的的所有期权到期日列表。

        由 trading_operations.py 直接调用。
        """
        lb_symbol = f"{underlying_symbol}.US"
        return await self._quote_ctx.option_chain_expiry_date_list(lb_symbol)

    # ------------------------------------------------------------------
    # Ticker 行情（单个合约）
    # ------------------------------------------------------------------

    async def get_ticker_for_stock(
        self,
        symbol: str,
        primary_exchange: str,
        order_exchange: Optional[str] = None,
        generic_tick_list: str = "",
        required_fields: Optional[List[Any]] = None,
        optional_fields: Optional[List[Any]] = None,
    ) -> FakeTicker:
        """
        获取股票的 FakeTicker。

        数据来源：
        - quote() → last_done（最新成交价）
        - depth() → bid/ask（若可用）
        """
        lb_symbol = f"{symbol}.US"
        contract = build_stock_contract(symbol, primary_exchange)

        last = 0.0
        bid: Optional[float] = None
        ask: Optional[float] = None

        try:
            quotes = await self._quote_ctx.quote([lb_symbol])
            if quotes:
                last = float(quotes[0].last_done)
        except Exception as exc:
            log.warning(f"{symbol}: 获取 quote 失败: {exc}")

        try:
            depth = await self._quote_ctx.depth(lb_symbol)
            if depth.bids and depth.bids[0].price:
                bid = float(depth.bids[0].price)
            if depth.asks and depth.asks[0].price:
                ask = float(depth.asks[0].price)
        except Exception:
            pass  # depth 不可用时静默退回 last

        return build_stock_ticker(contract, last, bid=bid, ask=ask)

    async def get_ticker_for_contract(
        self,
        contract: FakeContract,
        generic_tick_list: str = "",
        required_fields: Optional[List[Any]] = None,
        optional_fields: Optional[List[Any]] = None,
    ) -> FakeTicker:
        """
        获取期权合约的 FakeTicker（含 greeks）。

        数据来源：
        - calc_indexes() → last_done, delta, gamma, theta, vega, rho, IV, OI
        - depth()        → bid/ask（若可用，用于 midpoint()）

        若 delta 为 None（流动性差的合约），build_fake_ticker() 会用 Black-Scholes fallback。
        """
        lb_symbol = contract.lb_symbol()
        indexes = _get_greeks_indexes()

        last_done: Optional[float] = None
        delta: Optional[float] = None
        gamma: Optional[float] = None
        theta: Optional[float] = None
        vega: Optional[float] = None
        rho: Optional[float] = None
        implied_vol: Optional[float] = None
        open_interest: Optional[float] = None
        bid: Optional[float] = None
        ask: Optional[float] = None

        try:
            calcs = await self._quote_ctx.calc_indexes([lb_symbol], indexes)
            if calcs:
                c = calcs[0]
                last_done = decimal_to_float(c.last_done)
                delta = decimal_to_float(c.delta)
                gamma = decimal_to_float(c.gamma)
                theta = decimal_to_float(c.theta)
                vega = decimal_to_float(c.vega)
                rho = decimal_to_float(c.rho)
                implied_vol = decimal_to_float(c.implied_volatility)
                open_interest = float(c.open_interest) if c.open_interest else None
        except Exception as exc:
            log.warning(f"{contract.symbol}: calc_indexes 失败: {exc}")

        # option_quote fallback：提供 last_done / IV / OI（calc_indexes 返回 null 时使用）
        try:
            opt_quotes = await self._quote_ctx.option_quote([lb_symbol])
            if opt_quotes:
                q = opt_quotes[0]
                if not last_done:
                    last_done = decimal_to_float(q.last_done) or None
                if not implied_vol:
                    implied_vol = decimal_to_float(q.implied_volatility) or None
                if not open_interest and q.open_interest:
                    open_interest = float(q.open_interest)
        except Exception as exc:
            pass  # option_quote 失败时静默继续

        try:
            depth = await self._quote_ctx.depth(lb_symbol)
            if depth.bids and depth.bids[0].price:
                bid = float(depth.bids[0].price)
            if depth.asks and depth.asks[0].price:
                ask = float(depth.asks[0].price)
        except Exception:
            pass  # depth 不可用时静默退回 last_done

        return build_fake_ticker(
            contract=contract,
            last_done=last_done,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            rho=rho,
            implied_vol=implied_vol,
            open_interest=open_interest,
            bid=bid,
            ask=ask,
            risk_free_rate=self.config.longbridge.risk_free_rate,
        )

    async def get_underlying_hist_vol(
        self, symbol: str, window: int = 21
    ) -> Optional[float]:
        """
        计算标的历史波动率（年化）。

        使用近 (window+5) 个交易日的日收盘价，计算对数收益率标准差并年化。
        当 calc_indexes() 无法返回 IV（如无 USOption 行情订阅）时，
        用作 Black-Scholes delta 与理论价格计算的 fallback 波动率。

        仅需 Nasdaq Basic 订阅即可获取 history_candlesticks_by_date()。
        """
        from longbridge_wheel.greeks import build_stock_contract

        try:
            fake_stock = build_stock_contract(symbol)
            candles = await self.request_historical_data(
                fake_stock, f"{window + 5} D"
            )
            if not candles or len(candles) < 5:
                return None
            closes = [float(c.close) for c in candles if c.close and float(c.close) > 0]
            closes = closes[-window:]
            if len(closes) < 5:
                return None
            log_returns = [
                math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes))
            ]
            if not log_returns:
                return None
            mean = sum(log_returns) / len(log_returns)
            variance = sum((r - mean) ** 2 for r in log_returns) / max(
                len(log_returns) - 1, 1
            )
            annual_vol = math.sqrt(variance) * math.sqrt(252)
            return float(annual_vol) if annual_vol > 0 else None
        except Exception as exc:
            log.warning(f"{symbol}: 历史波动率计算失败: {exc}")
            return None

    async def get_tickers_for_contracts(
        self,
        underlying_symbol: str,
        contracts: List[FakeContract],
        generic_tick_list: str = "",
        required_fields: Optional[List[Any]] = None,
        optional_fields: Optional[List[Any]] = None,
        hist_vol: Optional[float] = None,
    ) -> List[FakeTicker]:
        """
        批量获取合约的 FakeTicker 列表。

        优化：一次 calc_indexes() 批量获取所有合约的 greeks，
        避免逐个调用（60 个合约 → 1 次 API 调用）。

        注意：depth() 仍为逐个调用，较慢且可能受订阅限制；
        批量场景下 bid/ask 回退到 last_done（midpoint() 使用 last_done fallback）。
        """
        if not contracts:
            return []

        lb_symbols = [c.lb_symbol() for c in contracts]
        indexes = _get_greeks_indexes()

        # 批量获取 greeks
        calc_map: Dict[str, Any] = {}
        try:
            calcs = await self._quote_ctx.calc_indexes(lb_symbols, indexes)
            calc_map = {c.symbol: c for c in calcs}
        except Exception as exc:
            log.warning(
                f"{underlying_symbol}: 批量 calc_indexes 失败: {exc}，"
                "将使用空 greeks"
            )

        # 批量获取 option_quote：提供 last_done / IV / OI，
        # 作为 calc_indexes 返回 null（无 USOption 订阅）时的 fallback
        quote_map: Dict[str, Any] = {}
        try:
            opt_quotes = await self._quote_ctx.option_quote(lb_symbols)
            quote_map = {q.symbol: q for q in opt_quotes}
        except Exception as exc:
            log.warning(
                f"{underlying_symbol}: 批量 option_quote 失败: {exc}，"
                "将跳过 last_done fallback"
            )

        tickers: List[FakeTicker] = []
        for contract in contracts:
            lb_sym = contract.lb_symbol()
            calc = calc_map.get(lb_sym)
            quote = quote_map.get(lb_sym)

            # last_done: 优先 calc_indexes，fallback 到 option_quote
            last_done_val = decimal_to_float(calc.last_done) if calc else None
            if not last_done_val and quote is not None:
                last_done_val = decimal_to_float(quote.last_done) or None

            # implied_vol: 同上
            implied_vol_val = decimal_to_float(calc.implied_volatility) if calc else None
            if not implied_vol_val and quote is not None:
                implied_vol_val = decimal_to_float(quote.implied_volatility) or None

            # open_interest: 同上
            open_interest_val = float(calc.open_interest) if calc and calc.open_interest else None
            if not open_interest_val and quote is not None and quote.open_interest:
                open_interest_val = float(quote.open_interest)

            ticker = build_fake_ticker(
                contract=contract,
                last_done=last_done_val,
                delta=decimal_to_float(calc.delta) if calc else None,
                gamma=decimal_to_float(calc.gamma) if calc else None,
                theta=decimal_to_float(calc.theta) if calc else None,
                vega=decimal_to_float(calc.vega) if calc else None,
                rho=decimal_to_float(calc.rho) if calc else None,
                implied_vol=implied_vol_val,
                open_interest=open_interest_val,
                bid=None,   # 批量场景不逐个调用 depth()
                ask=None,
                risk_free_rate=self.config.longbridge.risk_free_rate,
                hist_vol=hist_vol,
            )
            tickers.append(ticker)

        return tickers

    # ------------------------------------------------------------------
    # 订单管理
    # ------------------------------------------------------------------

    async def place_order(
        self, contract: FakeContract, order: LimitOrder
    ) -> LBTrade:
        """
        提交限价单，返回 LBTrade 封装。

        映射：
            IBKR "Adaptive Patient" 算法 → LB 标准限价单（OrderType.LO）
            ib_async 同步返回 Trade     → async 返回 LBTrade

        注意：lmtPrice 取绝对值（卖出价格为正数）。
        """
        from longbridge.openapi import (
            OrderSide,
            OrderType,
            OutsideRTH,
            TimeInForceType,
        )

        lb_symbol = contract.lb_symbol()
        side = OrderSide.Sell if order.action == "SELL" else OrderSide.Buy
        qty = Decimal(str(int(order.totalQuantity)))
        price = Decimal(str(abs(order.lmtPrice)))
        order_ref = getattr(order, "orderRef", None) or ""

        await asyncio.sleep(0.02)  # 交易 API 速率限制
        resp = await self._trade_ctx.submit_order(
            symbol=lb_symbol,
            order_type=OrderType.LO,
            side=side,
            submitted_quantity=qty,
            time_in_force=TimeInForceType.Day,
            submitted_price=price,
            outside_rth=OutsideRTH.RTHOnly,
            remark=order_ref[:64] if order_ref else None,
        )

        trade = LBTrade(
            order_id=resp.order_id,
            contract=contract,
            order=order,
        )
        self._register_trade(trade)

        log.info(
            f"{contract.symbol}: 订单已提交 order_id={resp.order_id} "
            f"action={order.action} qty={order.totalQuantity} "
            f"price={order.lmtPrice}"
        )
        return trade

    async def cancel_order(self, order_id: str) -> None:
        """
        撤销订单。

        参数 order_id：LB 订单 ID（字符串）。
        注意：与 IBKR 版本不同，此处接受 order_id 而非 Order 对象。
        """
        await asyncio.sleep(0.02)
        try:
            await self._trade_ctx.cancel_order(order_id)
            log.info(f"cancel_order: 已撤销订单 order_id={order_id}")
        except Exception as exc:
            log.warning(f"cancel_order: 撤销 {order_id} 失败: {exc}")

    async def replace_order(
        self,
        order_id: str,
        quantity: Decimal,
        price: Decimal,
    ) -> None:
        """
        修改订单价格（replace，不取消+重建）。

        优点：避免撤单到重下之间的空窗期（对期权尤其重要）。
        LB replace_order() 保留原 order_id，只更新数量和价格。
        """
        await asyncio.sleep(0.02)
        await self._trade_ctx.replace_order(
            order_id=order_id,
            quantity=quantity,
            price=price,
        )
        log.info(
            f"replace_order: 已修改订单 order_id={order_id} "
            f"qty={quantity} price={price}"
        )

    async def open_trades(self) -> List[LBTrade]:
        """
        获取今日未完结订单，封装为 LBTrade 列表。

        用于 portfolio_manager.initialize_account() 中撤销现有订单。

        LB today_orders() 过滤条件：New / PartialFilled / WaitToNew / Replaced。
        """
        from longbridge.openapi import Market, OrderStatus, OrderSide

        await asyncio.sleep(0.02)
        orders = await self._trade_ctx.today_orders(
            status=[
                OrderStatus.New,
                OrderStatus.PartialFilled,
                OrderStatus.WaitToNew,
                OrderStatus.Replaced,
                OrderStatus.WaitToReplace,
                OrderStatus.PendingReplace,
            ],
            market=Market.US,
        )

        trades: List[LBTrade] = []
        for order in orders:
            contract = self._lb_symbol_to_contract(order.symbol)
            if contract is None:
                continue

            # 重建 LimitOrder（供 print_summary 等显示用）
            # OrderSide も同じ Rust singleton pattern
            from longbridge.openapi import OrderSide as _OS
            side_name = "Sell" if order.side is _OS.Sell else "Buy"
            action = "SELL" if side_name == "Sell" else "BUY"
            price_val = float(order.price) if order.price else 0.0
            qty_val = float(order.quantity)
            lb_order = LimitOrder(
                action=action,
                totalQuantity=qty_val,
                lmtPrice=price_val,
            )

            trade = LBTrade(
                order_id=order.order_id,
                contract=contract,
                order=lb_order,
            )
            status_name = _lb_status_name(order.status)
            trade.orderStatus.status = status_name
            trade.orderStatus.filled = float(order.executed_quantity)
            trade.orderStatus.remaining = qty_val - float(order.executed_quantity)
            if order.executed_price:
                trade.orderStatus.avgFillPrice = float(order.executed_price)

            self._register_trade(trade)
            trades.append(trade)

        return trades

    # ------------------------------------------------------------------
    # 历史数据
    # ------------------------------------------------------------------

    async def request_historical_data(
        self,
        contract: FakeContract,
        duration: str,
    ) -> List[Any]:
        """
        获取历史 K 线数据，返回 LB Candlestick 列表。

        duration 格式（来自 thetagang）：
            "1 Y"  → 1 年
            "6 M"  → 6 个月
            "252 D" → 252 天

        regime_engine 用此数据判断趋势（EMA 交叉），
        所需字段：candlestick.close, .open, .high, .low, .volume, .timestamp
        """
        from longbridge.openapi import AdjustType, Period, TradeSessions

        lb_symbol = contract.lb_symbol()
        end_date = date.today()

        # 解析 duration 字符串
        parts = duration.strip().split()
        if len(parts) >= 2:
            n = int(parts[0])
            unit = parts[1].upper()
            if unit.startswith("Y"):
                try:
                    start_date = end_date.replace(year=end_date.year - n)
                except ValueError:
                    start_date = end_date - timedelta(days=n * 365)
            elif unit.startswith("M"):
                start_date = end_date - timedelta(days=n * 30)
            else:  # D / days
                start_date = end_date - timedelta(days=n)
        else:
            start_date = end_date - timedelta(days=365)

        try:
            candles = await self._quote_ctx.history_candlesticks_by_date(
                symbol=lb_symbol,
                period=Period.Day,
                adjust_type=AdjustType.NoAdjust,
                start=start_date,
                end=end_date,
                trade_sessions=TradeSessions.Intraday,
            )
            if self.data_store:
                self.data_store.record_historical_bars(contract.symbol, "1 day", candles)
            return candles
        except Exception as exc:
            log.warning(f"{contract.symbol}: 获取历史 K 线失败: {exc}")
            return []

    async def request_executions(self, exec_filter: Any = None) -> List[Any]:
        """
        获取成交记录。

        v1：直接返回空列表。
        regime_engine 用此数据记录再平衡成交，空列表表示本 cycle 无新成交。
        """
        return []
