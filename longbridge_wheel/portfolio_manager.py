"""
portfolio_manager.py — Longbridge Wheel 主交易循环编排器

从 thetagang portfolio_manager.py 移植，主要变更：
1. 注入 LongbridgeBroker 替代 IBKR（构造函数不再需要 ib: IB 参数）
2. 移除 IBC/Watchdog/ib_async 专属逻辑
3. get_portfolio_positions() 简化（LB API 直接返回持仓，无需 IBKR 重试逻辑）
4. initialize_account() 变为 async（LB open_trades() 是异步的）
5. submit_orders() 变为 async（LB place_order() 是异步的）
6. adjust_prices() 使用 asyncio.sleep 等待，不依赖 IBKR wait_for_orders_complete
7. 移除 set_market_data_type()（LB 只有实时数据）
8. cancel_order() 接受 trade.order_id 而非 trade.order
"""

from __future__ import annotations

import asyncio
import math
import random
from asyncio import Future
from datetime import date, datetime
from typing import Any, Coroutine, Dict, List, Optional, Tuple, cast

import numpy as np
from rich.console import Group
from rich.panel import Panel
from rich.table import Table

from longbridge_wheel import log
from longbridge_wheel.compat import AccountValue, LimitOrder, PortfolioItem
from longbridge_wheel.compat import Option, Stock
from longbridge_wheel.config import (
    CANONICAL_STAGE_ORDER,
    DEFAULT_RUN_STRATEGIES,
    Config,
    RunConfig,
    enabled_stage_ids_from_run,
    stage_enabled_map_from_run,
)
from longbridge_wheel.db import DataStore
from longbridge_wheel.fmt import dfmt, ffmt, ifmt, pfmt
from longbridge_wheel.greeks import FakeContract, FakeTicker
from longbridge_wheel.ibkr import RequiredFieldValidationError, TickerField
from longbridge_wheel.orders import Orders
from longbridge_wheel.strategies import (
    EquityStrategyDeps,
    OptionsStrategyDeps,
    PostStrategyDeps,
    run_equity_rebalance_stages,
    run_option_management_stages,
    run_option_write_stages,
    run_post_stages,
)
from longbridge_wheel.strategies.equity import EquityRebalanceService, RegimeRebalanceService
from longbridge_wheel.strategies.equity_engine import EquityRebalanceEngine
from longbridge_wheel.strategies.options import OptionsManageService, OptionsWriteService
from longbridge_wheel.strategies.options_engine import OptionsStrategyEngine
from longbridge_wheel.strategies.post_engine import PostStrategyEngine
from longbridge_wheel.strategies.regime_engine import RegimeRebalanceEngine
from longbridge_wheel.strategies.runtime_services import (
    EquityRuntimeServiceAdapter,
    OptionsRuntimeServiceAdapter,
    resolve_symbol_configs,
)
from longbridge_wheel.trades import Trades
from longbridge_wheel.trading_operations import OptionChainScanner, OrderOperations
from longbridge_wheel.util import (
    account_summary_to_dict,
    get_short_positions,
    midpoint_or_market_price,
    portfolio_positions_to_dict,
    position_pnl,
    would_increase_spread,
)
from longbridge_wheel.options import option_dte

# LongbridgeBroker 延迟导入（避免循环引用）
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from longbridge_wheel.broker import LongbridgeBroker


class PortfolioManager:

    @staticmethod
    def get_close_price(ticker: FakeTicker) -> float:
        """
        获取收盘价代理。

        LB 中 last_done 即最新成交价，用作收盘价（无单独收盘价字段）。
        """
        return ticker.marketPrice()

    def __init__(
        self,
        config: Config,
        broker: "LongbridgeBroker",
        completion_future: Future[bool],
        dry_run: bool,
        data_store: Optional[DataStore] = None,
        run_stage_flags: Optional[Dict[str, bool]] = None,
        run_stage_order: Optional[List[str]] = None,
    ) -> None:
        self.account_number = config.runtime.account.number
        self.config = config
        self.data_store = data_store
        self.ibkr = broker   # 与策略引擎中 self.ibkr 命名一致（无需修改策略引擎）
        self.completion_future = completion_future
        self.has_excess_calls: set[str] = set()
        self.has_excess_puts: set[str] = set()
        self.orders: Orders = Orders()
        self.trades: Trades = Trades(broker, data_store=data_store)
        self.target_quantities: Dict[str, int] = {}
        self.qualified_contracts: Dict[int, FakeContract] = {}
        self.dry_run = dry_run
        self.last_untracked_positions: Dict[str, List[PortfolioItem]] = {}
        self.order_ops = OrderOperations(
            config=self.config,
            account_number=self.account_number,
            orders=self.orders,
            data_store=self.data_store,
        )
        self.options_runtime_services = OptionsRuntimeServiceAdapter(
            get_symbols_fn=lambda: self.get_symbols(),
            get_primary_exchange_fn=lambda symbol: self.get_primary_exchange(symbol),
            get_buying_power_fn=lambda account_summary: self.get_buying_power(
                account_summary
            ),
            get_maximum_new_contracts_for_fn=(
                lambda symbol, primary_exchange, account_summary: (
                    self.get_maximum_new_contracts_for(
                        symbol, primary_exchange, account_summary
                    )
                )
            ),
            get_write_threshold_fn=lambda ticker, right: self.get_write_threshold(
                ticker, right
            ),
            get_close_price_fn=lambda ticker: self.get_close_price(ticker),
        )
        self.equity_runtime_services = EquityRuntimeServiceAdapter(
            get_primary_exchange_fn=lambda symbol: self.get_primary_exchange(symbol),
            get_buying_power_fn=lambda account_summary: self.get_buying_power(
                account_summary
            ),
            midpoint_or_market_price_fn=lambda ticker: self.midpoint_or_market_price(
                ticker
            ),
        )
        self.option_scanner = OptionChainScanner(
            config=self.config, ibkr=broker, order_ops=self.order_ops
        )
        self.options_engine = OptionsStrategyEngine(
            config=self.config,
            ibkr=broker,
            option_scanner=self.option_scanner,
            order_ops=self.order_ops,
            services=self.options_runtime_services,
            target_quantities=self.target_quantities,
            has_excess_puts=self.has_excess_puts,
            has_excess_calls=self.has_excess_calls,
            qualified_contracts=self.qualified_contracts,
        )
        self.regime_engine = RegimeRebalanceEngine(
            config=self.config,
            ibkr=broker,
            order_ops=self.order_ops,
            data_store=self.data_store,
            get_primary_exchange=self.get_primary_exchange,
            get_buying_power=self.get_regime_buying_power,
            now_provider=lambda: datetime.now(),
        )
        self.equity_engine = EquityRebalanceEngine(
            config=self.config,
            ibkr=broker,
            order_ops=self.order_ops,
            services=self.equity_runtime_services,
            regime_engine=self.regime_engine,
        )
        self.post_engine = PostStrategyEngine(
            config=self.config,
            ibkr=broker,
            order_ops=self.order_ops,
            option_scanner=self.option_scanner,
            orders=self.orders,
            qualified_contracts=self.qualified_contracts,
        )

        # 初始化 stage 开关
        if run_stage_flags is None:
            default_run = RunConfig(strategies=DEFAULT_RUN_STRATEGIES)
            self.run_stage_flags = stage_enabled_map_from_run(default_run)
            self.run_stage_order = enabled_stage_ids_from_run(default_run)
        else:
            self.run_stage_flags = dict(run_stage_flags)
            self.run_stage_order = [
                stage_id
                for stage_id in CANONICAL_STAGE_ORDER
                if self.run_stage_flags.get(stage_id, False)
            ]
        if run_stage_order is not None:
            self.run_stage_order = list(run_stage_order)
            enabled_set = set(self.run_stage_order)
            self.run_stage_flags = {
                stage_id: (stage_id in enabled_set)
                for stage_id in CANONICAL_STAGE_ORDER
            }

    # ------------------------------------------------------------------
    # Stage 管理
    # ------------------------------------------------------------------

    def stage_enabled(self, stage_id: str) -> bool:
        return bool(self.run_stage_flags.get(stage_id, False))

    def _options_strategy_deps(self, enabled_stages: set[str]) -> OptionsStrategyDeps:
        return OptionsStrategyDeps(
            enabled_stages=enabled_stages,
            write_service=cast(OptionsWriteService, self.options_engine),
            manage_service=cast(OptionsManageService, self.options_engine),
        )

    def _sync_options_engine_state(self) -> None:
        self.options_engine.target_quantities = self.target_quantities
        self.options_engine.has_excess_puts = self.has_excess_puts
        self.options_engine.has_excess_calls = self.has_excess_calls

    def _equity_strategy_deps(self, enabled_stages: set[str]) -> EquityStrategyDeps:
        return EquityStrategyDeps(
            enabled_stages=enabled_stages,
            regime_rebalance_enabled=bool(
                self.config.strategies.regime_rebalance.enabled
            ),
            regime_service=cast(RegimeRebalanceService, self.equity_engine),
            rebalance_service=cast(EquityRebalanceService, self.equity_engine),
        )

    def _post_strategy_deps(self, enabled_stages: set[str]) -> PostStrategyDeps:
        return PostStrategyDeps(
            enabled_stages=enabled_stages,
            service=self.post_engine,
        )

    # ------------------------------------------------------------------
    # 持仓工具
    # ------------------------------------------------------------------

    def get_short_calls(
        self, portfolio_positions: Dict[str, List[PortfolioItem]]
    ) -> List[PortfolioItem]:
        return self.get_short_contracts(portfolio_positions, "C")

    def get_short_puts(
        self, portfolio_positions: Dict[str, List[PortfolioItem]]
    ) -> List[PortfolioItem]:
        return self.get_short_contracts(portfolio_positions, "P")

    def _regime_rebalance_symbols(self) -> set[str]:
        regime_rebalance = self.config.strategies.regime_rebalance
        if not regime_rebalance.enabled:
            return set()
        return set(regime_rebalance.symbols)

    def options_trading_enabled(self) -> bool:
        regime_rebalance = self.config.strategies.regime_rebalance
        return not (regime_rebalance.enabled and regime_rebalance.shares_only)

    def get_short_contracts(
        self, portfolio_positions: Dict[str, List[PortfolioItem]], right: str
    ) -> List[PortfolioItem]:
        ret: List[PortfolioItem] = []
        for symbol in portfolio_positions:
            ret = ret + get_short_positions(portfolio_positions[symbol], right)
        return ret

    async def put_is_itm(self, contract: FakeContract) -> bool:
        return await self.options_engine.put_is_itm(contract)

    def position_can_be_closed(
        self, position: PortfolioItem, table: Table
    ) -> bool:
        return self.options_engine.position_can_be_closed(position, table)

    def put_can_be_closed(self, put: PortfolioItem, table: Table) -> bool:
        return self.options_engine.put_can_be_closed(put, table)

    async def put_can_be_rolled(self, put: PortfolioItem, table: Table) -> bool:
        return await self.options_engine.put_can_be_rolled(put, table)

    async def call_is_itm(self, contract: FakeContract) -> bool:
        return await self.options_engine.call_is_itm(contract)

    def call_can_be_closed(self, call: PortfolioItem, table: Table) -> bool:
        return self.options_engine.call_can_be_closed(call, table)

    async def call_can_be_rolled(self, call: PortfolioItem, table: Table) -> bool:
        return await self.options_engine.call_can_be_rolled(call, table)

    def get_symbols(self) -> List[str]:
        return list(self.config.portfolio.symbols.keys())

    def filter_positions(
        self, portfolio_positions: List[PortfolioItem]
    ) -> List[PortfolioItem]:
        filtered_positions, _ = self.partition_positions(portfolio_positions)
        return filtered_positions

    def partition_positions(
        self, portfolio_positions: List[PortfolioItem]
    ) -> Tuple[List[PortfolioItem], List[PortfolioItem]]:
        symbols = self.get_symbols()
        tracked_positions: List[PortfolioItem] = []
        untracked_positions: List[PortfolioItem] = []
        for item in portfolio_positions:
            if item.account != self.account_number or item.position == 0:
                continue
            if (
                item.contract.symbol in symbols
                or item.contract.symbol == "VIX"
                or item.contract.symbol
                == self.config.strategies.cash_management.cash_fund
            ):
                tracked_positions.append(item)
            else:
                untracked_positions.append(item)
        return (tracked_positions, untracked_positions)

    async def get_portfolio_positions(self) -> Dict[str, List[PortfolioItem]]:
        """
        获取当前持仓，以 symbol 为键分组。

        LB 版本：直接调用 broker.portfolio()，无需 IBKR 的多次重试逻辑。
        """
        self.last_untracked_positions = {}
        portfolio_positions = await self.ibkr.portfolio(account=self.account_number)
        filtered_positions, untracked_positions = self.partition_positions(
            portfolio_positions
        )
        portfolio_by_symbol = portfolio_positions_to_dict(filtered_positions)
        self.last_untracked_positions = portfolio_positions_to_dict(untracked_positions)
        return portfolio_by_symbol

    # ------------------------------------------------------------------
    # 账户初始化（async）
    # ------------------------------------------------------------------

    async def initialize_account(self) -> None:
        """
        初始化：撤销本账户中未完结的相关订单（若 cancel_orders=True）。

        LB 版本：open_trades() 是 async，cancel_order() 接受 order_id 字符串。
        """
        if not self.config.runtime.account.cancel_orders:
            return

        open_trades = await self.ibkr.open_trades()
        for trade in open_trades:
            if not trade.isDone() and (
                trade.contract.symbol in self.get_symbols()
                or (
                    self.config.strategies.vix_call_hedge.enabled
                    and trade.contract.symbol == "VIX"
                )
                or (
                    self.config.strategies.cash_management.enabled
                    and trade.contract.symbol
                    == self.config.strategies.cash_management.cash_fund
                )
            ):
                log.warning(
                    f"{trade.contract.symbol}: 撤销现有订单 "
                    f"order_id={trade.order_id}"
                )
                await self.ibkr.cancel_order(trade.order_id)

    # ------------------------------------------------------------------
    # 账户摘要
    # ------------------------------------------------------------------

    async def summarize_account(
        self,
    ) -> Tuple[
        Dict[str, AccountValue],
        Dict[str, List[PortfolioItem]],
    ]:
        account_summary = await self.ibkr.account_summary(self.account_number)
        account_summary = account_summary_to_dict(account_summary)

        if "NetLiquidation" not in account_summary:
            raise RuntimeError(
                f"账户 {self.config.runtime.account.number} 无效（未返回账户数据）"
            )

        table = Table(title="Account summary")
        table.add_column("Item")
        table.add_column("Value", justify="right")
        table.add_row(
            "Net liquidation", dfmt(account_summary["NetLiquidation"].value, 0)
        )
        table.add_row(
            "Excess liquidity", dfmt(account_summary["ExcessLiquidity"].value, 0)
        )
        table.add_row("Initial margin", dfmt(account_summary["InitMarginReq"].value, 0))
        table.add_row(
            "Maintenance margin", dfmt(account_summary["FullMaintMarginReq"].value, 0)
        )
        table.add_row("Buying power", dfmt(account_summary["BuyingPower"].value, 0))
        table.add_row("Total cash", dfmt(account_summary["TotalCashValue"].value, 0))
        table.add_row("Cushion", pfmt(account_summary["Cushion"].value, 0))
        table.add_section()
        table.add_row(
            "Target buying power usage",
            dfmt(self.get_buying_power(account_summary), 0),
        )
        log.print(Panel(table))

        portfolio_positions = await self.get_portfolio_positions()
        untracked_positions = self.last_untracked_positions

        if self.data_store:
            self.data_store.record_account_snapshot(account_summary)
            combined_positions: Dict[str, List[PortfolioItem]] = dict(
                portfolio_positions
            )
            for symbol, positions in untracked_positions.items():
                if symbol in combined_positions:
                    combined_positions[symbol].extend(positions)
                else:
                    combined_positions[symbol] = positions
            self.data_store.record_positions_snapshot(combined_positions)

        position_values: Dict[int, Dict[str, str]] = {}

        async def is_itm(pos: PortfolioItem) -> str:
            if isinstance(pos.contract, Option):
                if pos.contract.right.startswith("C") and await self.call_is_itm(
                    pos.contract
                ):
                    return "Y"
                if pos.contract.right.startswith("P") and await self.put_is_itm(
                    pos.contract
                ):
                    return "Y"
            return ""

        async def load_position_task(pos: PortfolioItem) -> None:
            qty = pos.position
            if isinstance(qty, float):
                qty_display = ifmt(int(qty)) if qty.is_integer() else ffmt(qty, 4)
            else:
                qty_display = ifmt(int(qty))
            position_values[pos.contract.conId] = {
                "qty": qty_display,
                "mktprice": dfmt(pos.marketPrice),
                "avgprice": dfmt(pos.averageCost),
                "value": dfmt(pos.marketValue, 0),
                "cost": dfmt(pos.averageCost * pos.position, 0),
                "unrealized": dfmt(pos.unrealizedPNL, 0),
                "p&l": pfmt(position_pnl(pos), 1),
                "itm?": await is_itm(pos),
            }
            if isinstance(pos.contract, Option):
                position_values[pos.contract.conId]["avgprice"] = dfmt(
                    pos.averageCost / float(pos.contract.multiplier)
                )
                position_values[pos.contract.conId]["strike"] = dfmt(
                    pos.contract.strike
                )
                position_values[pos.contract.conId]["dte"] = str(
                    option_dte(pos.contract.lastTradeDateOrContractMonth)
                )
                position_values[pos.contract.conId]["exp"] = str(
                    pos.contract.lastTradeDateOrContractMonth
                )

        tasks: List[Coroutine[Any, Any, None]] = []
        for _, positions in portfolio_positions.items():
            for position in positions:
                tasks.append(load_position_task(position))
        for _, positions in untracked_positions.items():
            for position in positions:
                tasks.append(load_position_task(position))
        await log.track_async(tasks, "Loading portfolio positions...")

        table = Table(title="Portfolio positions", collapse_padding=True)
        table.add_column("Symbol")
        table.add_column("R")
        table.add_column("Qty", justify="right")
        table.add_column("MktPrice", justify="right")
        table.add_column("AvgPrice", justify="right")
        table.add_column("Value", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Unrealized P&L", justify="right")
        table.add_column("P&L", justify="right")
        table.add_column("Strike", justify="right")
        table.add_column("Exp", justify="right")
        table.add_column("DTE", justify="right")
        table.add_column("ITM?")

        def getval(col: str, conId: int) -> str:
            return position_values[conId][col]

        def add_symbol_positions(
            symbol: str, positions: List[PortfolioItem]
        ) -> None:
            table.add_row(symbol)
            sorted_positions = sorted(
                positions,
                key=lambda p: (
                    option_dte(p.contract.lastTradeDateOrContractMonth)
                    if isinstance(p.contract, Option)
                    else -1
                ),
            )
            for pos in sorted_positions:
                conId = pos.contract.conId
                if isinstance(pos.contract, Stock):
                    table.add_row(
                        "",
                        "S",
                        getval("qty", conId),
                        getval("mktprice", conId),
                        getval("avgprice", conId),
                        getval("value", conId),
                        getval("cost", conId),
                        getval("unrealized", conId),
                        getval("p&l", conId),
                    )
                elif isinstance(pos.contract, Option):
                    table.add_row(
                        "",
                        pos.contract.right,
                        getval("qty", conId),
                        getval("mktprice", conId),
                        getval("avgprice", conId),
                        getval("value", conId),
                        getval("cost", conId),
                        getval("unrealized", conId),
                        getval("p&l", conId),
                        getval("strike", conId),
                        getval("exp", conId),
                        getval("dte", conId),
                        getval("itm?", conId),
                    )

        first = True
        for symbol, position in portfolio_positions.items():
            if not first:
                table.add_section()
            first = False
            add_symbol_positions(symbol, position)

        if untracked_positions:
            table.add_section()
            table.add_row("Not tracked")
            table.add_section()
            first_untracked = True
            for symbol, position in untracked_positions.items():
                if not first_untracked:
                    table.add_section()
                first_untracked = False
                add_symbol_positions(symbol, position)

        log.print(table)
        return (account_summary, portfolio_positions)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def manage(self) -> None:
        """
        执行一次完整的 Wheel 策略循环。

        包括：账户初始化 → 账户摘要 → 期权写入/管理 → 股票再平衡
             → 下单（或 dry-run 打印）→ 价格调整
        """
        had_error = False
        try:
            if self.data_store:
                self.data_store.record_event("run_start", {"dry_run": self.dry_run})

            await self.initialize_account()
            (account_summary, portfolio_positions) = await self.summarize_account()

            options_enabled = self.options_trading_enabled()
            enabled_stages = set(self.run_stage_order)
            stage_index = {
                stage_id: idx for idx, stage_id in enumerate(self.run_stage_order)
            }
            close_stage_handled = False
            options_disabled_notice_logged = False
            positions_might_be_stale = False

            write_stage_ids = {"options_write_puts", "options_write_calls"}
            management_stage_ids = {
                "options_roll_positions",
                "options_close_positions",
            }
            post_stage_ids = {"post_vix_call_hedge", "post_cash_management"}
            option_stage_ids = write_stage_ids | management_stage_ids
            refresh_before_stage_ids = management_stage_ids | post_stage_ids
            pre_management_trade_stage_ids = {
                "options_write_puts",
                "options_write_calls",
                "equity_regime_rebalance",
                "equity_buy_rebalance",
                "equity_sell_rebalance",
            }

            for stage_id in self.run_stage_order:
                if stage_id in option_stage_ids and not options_enabled:
                    if not options_disabled_notice_logged:
                        log.notice(
                            "Regime rebalancing shares-only enabled; "
                            "skipping option writes and rolls."
                        )
                        options_disabled_notice_logged = True
                    continue

                if stage_id in refresh_before_stage_ids and positions_might_be_stale:
                    portfolio_positions = await self.get_portfolio_positions()
                    positions_might_be_stale = False

                if stage_id in write_stage_ids:
                    await run_option_write_stages(
                        self._options_strategy_deps({stage_id}),
                        account_summary,
                        portfolio_positions,
                        options_enabled,
                    )
                elif stage_id == "options_roll_positions":
                    if (
                        "options_close_positions" in enabled_stages
                        and stage_index[stage_id]
                        < stage_index["options_close_positions"]
                    ):
                        await run_option_management_stages(
                            self._options_strategy_deps(
                                {
                                    "options_roll_positions",
                                    "options_close_positions",
                                }
                            ),
                            account_summary,
                            portfolio_positions,
                            options_enabled,
                        )
                        close_stage_handled = True
                    else:
                        await run_option_management_stages(
                            self._options_strategy_deps({"options_roll_positions"}),
                            account_summary,
                            portfolio_positions,
                            options_enabled,
                        )
                elif stage_id == "options_close_positions":
                    if close_stage_handled:
                        continue
                    await run_option_management_stages(
                        self._options_strategy_deps({"options_close_positions"}),
                        account_summary,
                        portfolio_positions,
                        options_enabled,
                    )
                elif stage_id in {
                    "equity_regime_rebalance",
                    "equity_buy_rebalance",
                    "equity_sell_rebalance",
                }:
                    await run_equity_rebalance_stages(
                        self._equity_strategy_deps({stage_id}),
                        account_summary,
                        portfolio_positions,
                    )
                elif stage_id in post_stage_ids:
                    await run_post_stages(
                        self._post_strategy_deps({stage_id}),
                        account_summary,
                        portfolio_positions,
                    )

                if stage_id in pre_management_trade_stage_ids:
                    positions_might_be_stale = True

            if self.dry_run:
                log.warning("Dry run enabled, no trades will be executed.")
                self.orders.print_summary()
            else:
                await self.submit_orders()
                await self.adjust_prices()
                self._log_open_orders()

            log.info("Longbridge Wheel done. See you next time!")

        except Exception:
            had_error = True
            log.error("Longbridge Wheel terminated with error...")
            raise

        finally:
            if self.data_store:
                self.data_store.record_event("run_end", {"success": not had_error})
            self.completion_future.set_result(True)

    def _log_open_orders(self) -> None:
        """打印本次循环结束时仍未完结的订单"""
        incomplete = [
            t for t in self.trades.records() if t and not t.isDone()
        ]
        if incomplete:
            symbols = ", ".join(
                f"{t.contract.symbol}(id={t.order_id}, "
                f"status={t.orderStatus.status})"
                for t in incomplete
            )
            log.info(f"本次循环结束时仍有未完结订单: {symbols}")

    # ------------------------------------------------------------------
    # 下单与价格调整（async）
    # ------------------------------------------------------------------

    async def submit_orders(self) -> None:
        """
        批量提交队列中的所有订单。

        LB 版本：submit_order() 是 async，需逐个 await。
        """
        for contract, order, intent_id in self.orders.records():
            await self.trades.submit_order(contract, order, intent_id=intent_id)
        self.trades.print_summary()

    async def adjust_prices(self) -> None:
        """
        对未成交订单进行重定价（价格 = (原价 + 中间价) / 2）。

        LB 版本：
        - 等待延迟（随机 [min, max] 秒）后检查
        - 使用 replace_order()（不取消+重建）
        - WebSocket 回调已实时更新订单状态，无需额外轮询
        """
        # 检查是否有需要调价的标的
        all_no_adjust = all(
            not self.config.portfolio.symbols[symbol].adjust_price_after_delay
            for symbol in self.config.portfolio.symbols
        )
        if all_no_adjust or self.trades.is_empty():
            log.warning("跳过订单价格调整...")
            return

        delay = random.randrange(
            self.config.runtime.orders.price_update_delay[0],
            self.config.runtime.orders.price_update_delay[1],
        )
        log.info(f"等待 {delay}s 后检查未成交订单...")
        await asyncio.sleep(delay)

        unfilled = [
            (idx, trade)
            for idx, trade in enumerate(self.trades.records())
            if trade
            and trade.contract.symbol in self.config.portfolio.symbols
            and self.config.portfolio.symbols[
                trade.contract.symbol
            ].adjust_price_after_delay
            and not trade.isDone()
        ]

        for idx, trade in unfilled:
            try:
                ticker = await self.ibkr.get_ticker_for_contract(trade.contract)
                (contract, order) = (trade.contract, trade.order)

                updated_price = np.sign(float(order.lmtPrice or 0)) * max(
                    [
                        (
                            self.config.runtime.orders.minimum_credit
                            if order.action == "BUY"
                            and float(order.lmtPrice or 0) <= 0.0
                            else 0.0
                        ),
                        math.fabs(
                            round(
                                (float(order.lmtPrice or 0) + ticker.midpoint()) / 2.0,
                                2,
                            )
                        ),
                    ]
                )

                if contract.symbol == "VIX":
                    updated_price = self.order_ops.round_vix_price(updated_price)

                if would_increase_spread(order, updated_price):
                    log.warning(
                        f"{contract.symbol}: 跳过重定价 "
                        f"old={dfmt(float(order.lmtPrice or 0))} "
                        f"new={dfmt(updated_price)}（会扩大价差）"
                    )
                    continue

                if float(order.lmtPrice or 0) != updated_price and np.sign(
                    float(order.lmtPrice or 0)
                ) == np.sign(updated_price):
                    log.info(
                        f"{contract.symbol}: 重定价 "
                        f"action={order.action} "
                        f"old={dfmt(float(order.lmtPrice or 0))} "
                        f"new={dfmt(updated_price)}"
                    )
                    new_order = LimitOrder(
                        order.action,
                        order.totalQuantity,
                        float(updated_price),
                    )
                    await self.trades.submit_order(contract, new_order, idx)

            except (asyncio.TimeoutError, RuntimeError, RequiredFieldValidationError) as exc:
                log.warning(
                    f"{trade.contract.symbol}: 无法获取中间价，跳过重定价: {exc}"
                )
                if self.data_store:
                    self.data_store.record_event(
                        "order_price_adjustment_skipped",
                        {
                            "symbol": getattr(trade.contract, "symbol", ""),
                            "secType": getattr(trade.contract, "secType", ""),
                            "reason": type(exc).__name__,
                        },
                    )
                continue

    # ------------------------------------------------------------------
    # 代理方法（转发给子引擎，保持向后兼容）
    # ------------------------------------------------------------------

    async def get_maximum_new_contracts_for(
        self,
        symbol: str,
        primary_exchange: str,
        account_summary: Dict[str, AccountValue],
    ) -> int:
        total_buying_power = self.get_buying_power(account_summary)
        max_buying_power = (
            self.config.strategies.wheel.defaults.target.maximum_new_contracts_percent
            * total_buying_power
        )
        ticker = await self.ibkr.get_ticker_for_stock(symbol, primary_exchange)
        price = midpoint_or_market_price(ticker)
        return max([1, round((max_buying_power / price) // 100)])

    def get_primary_exchange(self, symbol: str) -> str:
        return self.config.portfolio.symbols[symbol].primary_exchange

    def _buying_power_with_margin(
        self, account_summary: Dict[str, AccountValue], margin_usage: float
    ) -> int:
        return math.floor(float(account_summary["NetLiquidation"].value) * margin_usage)

    def _resolve_margin_usage(self, resolver_name: str) -> float:
        fallback_raw = self.config.runtime.account.margin_usage
        fallback = (
            float(fallback_raw)
            if isinstance(fallback_raw, (int, float))
            and not isinstance(fallback_raw, bool)
            else 1.0
        )
        resolver = getattr(self.config, resolver_name, None)
        if not callable(resolver):
            return fallback
        try:
            resolved = resolver()
        except Exception:
            return fallback
        if isinstance(resolved, bool) or not isinstance(resolved, (int, float)):
            return fallback
        return float(resolved)

    def get_wheel_buying_power(
        self, account_summary: Dict[str, AccountValue]
    ) -> int:
        margin_usage = self._resolve_margin_usage("wheel_margin_usage")
        return self._buying_power_with_margin(account_summary, margin_usage)

    def get_regime_buying_power(
        self, account_summary: Dict[str, AccountValue]
    ) -> int:
        margin_usage = self._resolve_margin_usage("regime_margin_usage")
        return self._buying_power_with_margin(account_summary, margin_usage)

    def get_buying_power(self, account_summary: Dict[str, AccountValue]) -> int:
        return self.get_wheel_buying_power(account_summary)

    def midpoint_or_market_price(self, ticker: FakeTicker) -> float:
        return float(midpoint_or_market_price(ticker))

    async def get_write_threshold(
        self, ticker: FakeTicker, right: str
    ) -> Tuple[float, float]:
        assert ticker.contract is not None
        close_price = self.get_close_price(ticker)
        absolute_daily_change = math.fabs(ticker.marketPrice() - close_price)

        threshold_sigma = self.config.get_write_threshold_sigma(
            ticker.contract.symbol, right
        )
        if threshold_sigma:
            hist_prices = await self.ibkr.request_historical_data(
                ticker.contract,
                self.config.strategies.wheel.defaults.constants.daily_stddev_window,
            )
            # LB Candlestick: .close 是 Decimal
            log_prices = np.log(
                np.array([float(p.close) for p in hist_prices])
            )
            stddev = np.std(np.diff(log_prices), ddof=1)
            return (
                close_price * (np.exp(stddev) - 1).astype(float) * threshold_sigma,
                absolute_daily_change,
            )

        threshold_perc = self.config.get_write_threshold_perc(
            ticker.contract.symbol, right
        )
        return (threshold_perc * close_price, absolute_daily_change)

    def format_weight_info(
        self,
        symbol: str,
        position_values: Dict[str, float],
        weight_base_value: float,
    ) -> Tuple[str, str]:
        symbol_configs = resolve_symbol_configs(
            self.config, context="portfolio weight formatting"
        )
        return self.options_engine.format_weight_info(
            symbol, position_values, weight_base_value, symbol_configs
        )

    # ------------------------------------------------------------------
    # 以下方法为直接转发给子引擎，与 thetagang 完全兼容
    # ------------------------------------------------------------------

    async def check_if_can_write_puts(self, account_summary, portfolio_positions):
        self._sync_options_engine_state()
        return await self.options_engine.check_if_can_write_puts(
            account_summary, portfolio_positions
        )

    async def check_for_uncovered_positions(self, account_summary, portfolio_positions):
        self._sync_options_engine_state()
        return await self.options_engine.check_for_uncovered_positions(
            account_summary, portfolio_positions
        )

    async def write_calls(self, calls):
        self._sync_options_engine_state()
        await self.options_engine.write_calls(calls)

    async def write_puts(self, puts):
        self._sync_options_engine_state()
        await self.options_engine.write_puts(puts)

    async def check_puts(self, portfolio_positions):
        return await self.options_engine.check_puts(portfolio_positions)

    async def check_calls(self, portfolio_positions):
        return await self.options_engine.check_calls(portfolio_positions)

    async def roll_puts(self, puts, account_summary):
        return await self.options_engine.roll_puts(puts, account_summary)

    async def roll_calls(self, calls, account_summary, portfolio_positions):
        return await self.options_engine.roll_calls(
            calls, account_summary, portfolio_positions
        )

    async def close_puts(self, puts):
        await self.options_engine.close_puts(puts)

    async def close_calls(self, calls):
        await self.options_engine.close_calls(calls)

    async def check_regime_rebalance_positions(self, account_summary, portfolio_positions):
        return await self.regime_engine.check_regime_rebalance_positions(
            account_summary, portfolio_positions
        )

    async def execute_regime_rebalance_orders(self, orders):
        await self.equity_engine.execute_regime_rebalance_orders(orders)

    async def check_buy_only_positions(self, account_summary, portfolio_positions):
        return await self.equity_engine.check_buy_only_positions(
            account_summary, portfolio_positions
        )

    async def execute_buy_orders(self, buy_orders):
        await self.equity_engine.execute_buy_orders(buy_orders)

    async def check_sell_only_positions(self, account_summary, portfolio_positions):
        return await self.equity_engine.check_sell_only_positions(
            account_summary, portfolio_positions
        )

    async def execute_sell_orders(self, sell_orders):
        await self.equity_engine.execute_sell_orders(sell_orders)

    async def do_vix_hedging(self, account_summary, portfolio_positions):
        await self.post_engine.do_vix_hedging(account_summary, portfolio_positions)

    def calc_pending_cash_balance(self) -> float:
        return self.post_engine.calc_pending_cash_balance()

    async def do_cashman(self, account_summary, portfolio_positions):
        await self.post_engine.do_cashman(account_summary, portfolio_positions)
