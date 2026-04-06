"""
trading_operations.py — OrderOperations + OptionChainScanner（LB 适配版）

与 thetagang 原版的主要差异：
1. OptionChainScanner：
   - 不调用 qualify_contracts()（LB symbol 已自我描述）
   - 使用 broker.get_chain_expiry_dates() + broker.get_chain_strikes_for_expiry() 构建合约列表
   - 使用 broker.get_tickers_for_contracts() 批量获取 greeks（单次 calc_indexes() 调用）
   - FakeOption 直接从 StrikePriceInfo.call_symbol / put_symbol 构建

2. OrderOperations：
   - 移除 IBKR algo 策略（algoStrategy / algoParams）
   - 保留 round_vix_price()、create_limit_order()、enqueue_order()
"""

from __future__ import annotations

import math
from datetime import date
from typing import Callable, List, Optional, Tuple, TYPE_CHECKING

from longbridge_wheel import log
from longbridge_wheel.compat import LimitOrder, TagValue
from longbridge_wheel.fmt import dfmt
from longbridge_wheel.greeks import FakeContract, FakeOption, FakeTicker, parse_option_symbol
from longbridge_wheel.options import option_dte
from longbridge_wheel.util import midpoint_or_market_price

if TYPE_CHECKING:
    from longbridge_wheel.broker import LongbridgeBroker
    from longbridge_wheel.config import Config
    from longbridge_wheel.db import DataStore
    from longbridge_wheel.orders import Orders


class NoValidContractsError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# OrderOperations — 限价单构建与入队
# ---------------------------------------------------------------------------

class OrderOperations:
    """
    封装限价单构建逻辑，与 thetagang 原版 OrderOperations 接口兼容。

    差异：
    - 移除 IBKR algo 策略（LB 使用标准限价单）
    - get_algo_strategy() / get_algo_params() 返回空值（不报错，保持兼容）
    """

    def __init__(
        self,
        *,
        config: "Config",
        account_number: str,
        orders: "Orders",
        data_store: Optional["DataStore"],
    ) -> None:
        self.config = config
        self.account_number = account_number
        self.orders = orders
        self.data_store = data_store

    def get_algo_strategy(self) -> str:
        """LB 不支持 IBKR 算法单；返回空字符串（兼容占位）"""
        return ""

    def algo_params_from(self, params: List[List[str]]) -> List[TagValue]:
        return [TagValue(p[0], p[1]) for p in params]

    def get_algo_params(self) -> List[TagValue]:
        """LB 不支持 IBKR 算法单；返回空列表（兼容占位）"""
        return []

    def get_order_exchange(self) -> str:
        return self.config.runtime.orders.exchange

    def round_vix_price(self, price: float) -> float:
        """VIX 期权价格取整（VIX ≥ 3.0 时精度 0.05，否则 0.01）"""
        if price >= 3.0:
            return round(price * 20) / 20
        return round(price * 100) / 100

    def create_limit_order(
        self,
        *,
        action: str,
        quantity: float,
        limit_price: float,
        algo_strategy: Optional[str] = None,
        algo_params: Optional[List[TagValue]] = None,
        use_default_algo: bool = True,
        tif: str = "DAY",
        order_ref: Optional[str] = None,
        transmit: bool = True,
        order_id: Optional[int] = None,
    ) -> LimitOrder:
        """
        构建 LimitOrder。

        LB 忽略 algo_strategy / algo_params（只使用标准限价单）。
        所有其他字段与 thetagang 原版相同。
        """
        kwargs = {
            "tif": tif,
            "account": self.account_number,
            "transmit": transmit,
        }
        if order_ref is not None:
            kwargs["orderRef"] = order_ref
        if order_id is not None:
            kwargs["orderId"] = order_id
        return LimitOrder(action, quantity, limit_price, **kwargs)

    def enqueue_order(
        self,
        contract: Optional[FakeContract],
        order: LimitOrder,
    ) -> None:
        """将订单加入待提交队列，同时记录 OrderIntent 到数据库"""
        if not contract:
            return
        intent_id = None
        if self.data_store:
            intent_id = self.data_store.record_order_intent(contract, order)
        self.orders.add_order(contract, order, intent_id)
        if self.data_store:
            self.data_store.record_event(
                "order_enqueued",
                {
                    "symbol": getattr(contract, "symbol", None),
                    "sec_type": getattr(contract, "secType", None),
                    "con_id": getattr(contract, "conId", None),
                    "exchange": getattr(contract, "exchange", None),
                    "currency": getattr(contract, "currency", None),
                    "action": getattr(order, "action", None),
                    "quantity": getattr(order, "totalQuantity", None),
                    "limit_price": getattr(order, "lmtPrice", None),
                    "order_type": getattr(order, "orderType", None),
                    "order_ref": getattr(order, "orderRef", None),
                    "intent_id": intent_id,
                },
                symbol=getattr(contract, "symbol", None),
            )


# ---------------------------------------------------------------------------
# OptionChainScanner — LB 批量期权链扫描
# ---------------------------------------------------------------------------

class OptionChainScanner:
    """
    期权链扫描器（LB 适配版）。

    核心流程（替代 thetagang 的 qualify_contracts + 60 次 get_ticker）：
    1. get_chain_expiry_dates() → 所有可用到期日（1 次 API 调用）
    2. 按 target_dte / max_dte / chain_expirations 过滤到期日
    3. 对每个选中的到期日调用 get_chain_strikes_for_expiry() → StrikePriceInfo 列表
    4. 从 StrikePriceInfo.call_symbol / put_symbol 构建 FakeOption 合约
    5. 按 valid_strike / chain_strikes 过滤行权价
    6. 一次 get_tickers_for_contracts() 批量获取 greeks（~1 次 calc_indexes 调用）
    7. 过滤 price_is_valid, delta_is_valid, minimum_open_interest
    8. 排序并返回最优合约的 FakeTicker
    """

    def __init__(
        self,
        *,
        config: "Config",
        ibkr: "LongbridgeBroker",
        order_ops: OrderOperations,
    ) -> None:
        self.config = config
        self.ibkr = ibkr
        self.order_ops = order_ops

    async def find_eligible_contracts(
        self,
        underlying: FakeContract,
        right: str,
        strike_limit: Optional[float],
        minimum_price: Callable[[], float],
        exclude_expirations_before: Optional[str] = None,
        exclude_exp_strike: Optional[Tuple[float, str]] = None,
        fallback_minimum_price: Optional[Callable[[], float]] = None,
        target_dte: Optional[int] = None,
        target_delta: Optional[float] = None,
    ) -> FakeTicker:
        """
        在期权链中搜索满足条件的最优合约，返回其 FakeTicker。

        参数：
            underlying    : 标的合约（FakeStock，含 symbol）
            right         : "C"（看涨）或 "P"（看跌）
            strike_limit  : 行权价上限（P）或下限（C），None 时用价格的 ±5%
            minimum_price : 合约最低信用额（callable，因为可能动态变化）
            exclude_expirations_before : 排除此日期之前的到期日（YYYYMMDD）
            exclude_exp_strike : 排除特定 (strike, expiry) 组合（rolling 时避免同合约）
            fallback_minimum_price : 如果主条件无合约时的 fallback 最低信用额
            target_dte    : 目标 DTE（覆盖 config）
            target_delta  : 目标 delta（覆盖 config）

        抛出 NoValidContractsError 如果没有合格合约。
        """
        symbol = underlying.symbol
        contract_target_dte = target_dte or self.config.get_target_dte(symbol)
        contract_target_delta = (
            target_delta or self.config.get_target_delta(symbol, right)
        )
        contract_max_dte = self.config.get_max_dte_for(symbol)
        chain_expirations_limit = self.config.runtime.option_chains.expirations
        chain_strikes_limit = self.config.runtime.option_chains.strikes

        log.notice(
            f"{symbol}: 搜索期权链 right={right} "
            f"strike_limit={strike_limit} "
            f"minimum_price={dfmt(minimum_price(), 3)} "
            f"target_dte={contract_target_dte} max_dte={contract_max_dte} "
            f"target_delta={contract_target_delta}，请稍候..."
        )

        # ------------------------------------------------------------------
        # 步骤 1：获取标的当前价格（用于 strike 过滤）
        # ------------------------------------------------------------------
        underlying_ticker = await self.ibkr.get_ticker_for_stock(
            symbol=symbol,
            primary_exchange=underlying.primaryExch or "",
        )
        underlying_price = midpoint_or_market_price(underlying_ticker)
        if underlying_price <= 0:
            raise NoValidContractsError(
                f"{symbol}: 无法获取标的价格，已跳过"
            )

        # ------------------------------------------------------------------
        # 步骤 2：过滤到期日
        # ------------------------------------------------------------------
        all_expiry_dates = await self.ibkr.get_chain_expiry_dates(symbol)
        if not all_expiry_dates:
            raise NoValidContractsError(f"{symbol}: 期权链没有到期日")

        min_dte = option_dte(exclude_expirations_before) if exclude_expirations_before else 0

        eligible_expiries: List[date] = []
        for expiry in sorted(all_expiry_dates):
            expiry_str = expiry.strftime("%Y%m%d")
            dte = option_dte(expiry_str)
            if dte < contract_target_dte:
                continue
            if dte < min_dte:
                continue
            if contract_max_dte and dte > contract_max_dte:
                continue
            eligible_expiries.append(expiry)

        eligible_expiries = eligible_expiries[:chain_expirations_limit]
        if not eligible_expiries:
            raise NoValidContractsError(
                f"{symbol}: 没有满足 DTE 条件的到期日"
            )

        # ------------------------------------------------------------------
        # 步骤 3 & 4：从 StrikePriceInfo 构建 FakeOption 列表
        # ------------------------------------------------------------------

        def valid_strike(strike: float) -> bool:
            if right.startswith("P") and strike_limit:
                return strike <= strike_limit
            elif right.startswith("P"):
                return strike <= underlying_price * 1.05
            elif right.startswith("C") and strike_limit:
                return strike >= strike_limit
            elif right.startswith("C"):
                return strike >= underlying_price * 0.95
            return False

        def nearest_strikes(strikes: List[float]) -> List[float]:
            if right.startswith("P"):
                return strikes[-chain_strikes_limit:]
            return strikes[:chain_strikes_limit]

        all_candidates: List[FakeOption] = []

        for expiry in eligible_expiries:
            expiry_str = expiry.strftime("%Y%m%d")
            try:
                strike_infos = await self.ibkr.get_chain_strikes_for_expiry(
                    symbol, expiry
                )
            except Exception as exc:
                log.warning(f"{symbol}: 获取 {expiry_str} 链失败: {exc}")
                continue

            # 按 valid_strike 过滤，再取最近 chain_strikes 个（按 price 升序排序）
            valid_strikes = sorted(
                (si for si in strike_infos
                 if si.standard and valid_strike(float(si.price))),
                key=lambda si: si.price,
            )
            selected = nearest_strikes(valid_strikes)

            for si in selected:
                lb_symbol = si.call_symbol if right.startswith("C") else si.put_symbol
                if not lb_symbol:
                    continue
                contract = parse_option_symbol(lb_symbol)
                if contract is None:
                    log.warning(
                        f"{symbol}: 无法解析期权 symbol={lb_symbol}，已跳过"
                    )
                    continue
                contract._underlying_price = underlying_price
                all_candidates.append(contract)

        if not all_candidates:
            raise NoValidContractsError(
                f"{symbol}: 过滤后没有可用的行权价"
            )

        # 排除指定 (strike, expiry) 组合（rolling 时避免 roll 到相同合约）
        if exclude_exp_strike:
            exc_strike, exc_exp = exclude_exp_strike
            all_candidates = [
                c for c in all_candidates
                if not (
                    c.lastTradeDateOrContractMonth == exc_exp
                    and math.isclose(c.strike, exc_strike)
                )
            ]

        log.info(
            f"{symbol}: 共找到 {len(all_candidates)} 个候选合约，"
            f"正在批量获取 greeks..."
        )

        # ------------------------------------------------------------------
        # 步骤 4.5：获取标的历史波动率（当 calc_indexes 无行情时用作 B-S fallback）
        # ------------------------------------------------------------------
        hist_vol = await self.ibkr.get_underlying_hist_vol(symbol)
        if hist_vol:
            log.info(
                f"{symbol}: 历史波动率 {hist_vol:.1%}（将作为 B-S fallback 波动率）"
            )
        else:
            log.debug(f"{symbol}: 历史波动率获取失败，B-S fallback 不可用")

        # ------------------------------------------------------------------
        # 步骤 5：批量获取 greeks（单次 calc_indexes 调用）
        # ------------------------------------------------------------------
        tickers = await self.ibkr.get_tickers_for_contracts(
            symbol, all_candidates, hist_vol=hist_vol
        )

        # ------------------------------------------------------------------
        # 步骤 6：过滤
        # ------------------------------------------------------------------

        def open_interest_is_valid(ticker: FakeTicker, min_oi: int) -> bool:
            if right.startswith("P"):
                return ticker.putOpenInterest >= min_oi
            return ticker.callOpenInterest >= min_oi

        def delta_is_valid(ticker: FakeTicker) -> bool:
            from longbridge_wheel.compat import util
            greeks = ticker.modelGreeks
            delta = greeks.delta if greeks is not None else None
            return (
                delta is not None
                and not util.isNan(delta)
                and abs(delta) <= contract_target_delta
            )

        def price_is_valid(ticker: FakeTicker) -> bool:
            from longbridge_wheel.compat import Option
            price = midpoint_or_market_price(ticker)
            if math.isnan(price) or price <= minimum_price():
                return False
            # 成本不能超过市价 + 标的价格（put 特有检查）
            if right.startswith("C"):
                return True
            contract = ticker.contract
            if isinstance(contract, Option) and contract.strike:
                return contract.strike <= price + underlying_price
            return True

        # 过滤价格
        tickers = [t for t in tickers if price_is_valid(t)]

        # 分离 delta 有效 / 无效的 ticker
        valid_tickers: List[FakeTicker] = []
        delta_reject: List[FakeTicker] = []
        for t in tickers:
            if delta_is_valid(t):
                valid_tickers.append(t)
            else:
                delta_reject.append(t)

        def filter_and_sort(
            tickers: List[FakeTicker],
            delta_ord_desc: bool,
        ) -> List[FakeTicker]:
            """按 open interest 过滤，再排序（delta 降序 → DTE 升序）"""
            min_oi = getattr(
                getattr(
                    getattr(self.config, "strategies", None),
                    "wheel",
                    None,
                ),
                "defaults",
                None,
            )
            if min_oi is not None:
                min_oi = getattr(
                    getattr(min_oi, "target", None), "minimum_open_interest", 0
                ) or 0
            else:
                min_oi = 0

            if min_oi > 0:
                # 若所有合约 OI 均为 0（无 USOption 行情订阅时 calc_indexes 返回 null），
                # 跳过 OI 过滤，避免误删所有候选合约
                has_oi_data = any(
                    t.callOpenInterest > 0 or t.putOpenInterest > 0
                    for t in tickers
                )
                if has_oi_data:
                    tickers = [
                        t for t in tickers if open_interest_is_valid(t, min_oi)
                    ]

            return sorted(
                sorted(
                    tickers,
                    key=lambda t: (
                        abs(t.modelGreeks.delta)
                        if t.modelGreeks and t.modelGreeks.delta
                        else 0
                    ),
                    reverse=delta_ord_desc,
                ),
                key=lambda t: (
                    option_dte(t.contract.lastTradeDateOrContractMonth)
                    if t.contract
                    else 0
                ),
            )

        # ------------------------------------------------------------------
        # 步骤 7：排序并选择最优合约
        # ------------------------------------------------------------------
        sorted_tickers = filter_and_sort(valid_tickers, delta_ord_desc=True)
        chosen: Optional[FakeTicker] = None

        if not sorted_tickers:
            # 没有满足 delta 条件的合约时，尝试从 delta_reject 中选择
            if not math.isclose(minimum_price(), 0.0):
                sorted_tickers = filter_and_sort(delta_reject, delta_ord_desc=False)
            if not sorted_tickers:
                raise NoValidContractsError(
                    f"{symbol}: 没有满足条件的合约"
                )
        elif fallback_minimum_price is not None:
            for t in sorted_tickers:
                if midpoint_or_market_price(t) > fallback_minimum_price():
                    chosen = t
                    break
            if chosen is None:
                sorted_tickers = sorted(
                    sorted_tickers,
                    key=midpoint_or_market_price,
                    reverse=True,
                )

        if chosen is None:
            chosen = sorted_tickers[0]

        if not chosen or not chosen.contract:
            raise RuntimeError(
                f"{symbol}: 选择合约时出现意外错误, chosen={chosen}"
            )

        # 步骤 8：用 depth() 刷新最优合约的真实买卖价，确保限价单反映市场实际价格
        # 批量扫描时不调用 depth()（太慢），只对最终选定合约做一次单独查询
        pre_refresh = chosen  # 保存批量计算的价格（含 B-S 理论价）
        try:
            refreshed = await self.ibkr.get_ticker_for_contract(chosen.contract)
            chosen = refreshed
            log.notice(
                f"{symbol}: 最优合约行情已刷新 "
                f"bid={refreshed.bid:.3f} ask={refreshed.ask:.3f} "
                f"midpoint={refreshed.midpoint():.3f}"
            )
        except Exception as exc:
            log.warning(f"{symbol}: 刷新最优合约行情失败，使用 B-S 估算价格: {exc}")

        final_price = midpoint_or_market_price(chosen)
        if math.isnan(final_price) or final_price <= 0:
            # 无 USOption 行情时回退到批量 B-S 理论价（hist_vol 来自历史 K 线，无需期权订阅）
            final_price = midpoint_or_market_price(pre_refresh)
            if math.isnan(final_price) or final_price <= 0:
                raise NoValidContractsError(
                    f"{symbol}: 最优合约价格不可用（bid/ask/last 均为 NaN 或 0），"
                    f"可能需要 USOption 行情订阅"
                )
            log.warning(
                f"{symbol}: 无实时行情，使用 B-S 估算价格下单: {final_price:.3f}"
            )
            chosen = pre_refresh

        log.notice(
            f"{symbol}: 找到最优合约 "
            f"strike={chosen.contract.strike} "
            f"dte={option_dte(chosen.contract.lastTradeDateOrContractMonth)} "
            f"price={dfmt(final_price, 3)}"
        )
        return chosen
