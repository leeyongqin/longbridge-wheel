"""
longbridge_wheel.py — 启动编排：配置加载 → 数据库初始化 → Broker 连接 → manage()

与 thetagang/thetagang.py 的主要差异：
1. 无 IBC / Watchdog / ib_async 事件循环
2. 使用 asyncio.run() 直接运行 manage() 协程（cron-based 单次执行）
3. LongbridgeBroker.setup() 建立 WebSocket 连接，teardown() 关闭
4. 支持 --verify-symbols 模式：打印样本期权 symbol 后退出
"""

from __future__ import annotations

import asyncio
from asyncio import Future
from pathlib import Path
from typing import Optional

import tomlkit

from longbridge_wheel import log
from longbridge_wheel.broker import LongbridgeBroker
from longbridge_wheel.config import Config, enabled_stage_ids_from_run, stage_enabled_map
from longbridge_wheel.db import DataStore, sqlite_db_path
from longbridge_wheel.exchange_hours import need_to_exit
from longbridge_wheel.portfolio_manager import PortfolioManager


def start(
    config_path: str,
    dry_run: bool = False,
    verify_symbols: bool = False,
) -> None:
    """
    同步入口：加载配置，启动异步事件循环，执行一次 manage() 后退出。

    参数：
        config_path    : thetagang.toml 路径
        dry_run        : True 时只打印计划，不实际下单
        verify_symbols : True 时打印期权 symbol 样本后退出
    """
    # ------------------------------------------------------------------
    # 1. 加载配置
    # ------------------------------------------------------------------
    raw_config = Path(config_path).read_text(encoding="utf-8")
    config_doc = tomlkit.parse(raw_config).unwrap()
    config = Config(**config_doc)

    run_stage_flags = stage_enabled_map(config)
    run_stage_order = enabled_stage_ids_from_run(config.run)

    config.display(config_path)

    # ------------------------------------------------------------------
    # 2. 初始化数据库（若配置启用）
    # ------------------------------------------------------------------
    data_store: Optional[DataStore] = None
    if config.runtime.database.enabled:
        db_url = config.runtime.database.resolve_url(config_path)
        sqlite_path = sqlite_db_path(db_url)
        if sqlite_path:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        data_store = DataStore(db_url, config_path, dry_run, raw_config)

    # ------------------------------------------------------------------
    # 3. 检查市场开市时间
    # ------------------------------------------------------------------
    if need_to_exit(config.runtime.exchange_hours):
        log.info("当前非开市时间，退出。")
        return

    # ------------------------------------------------------------------
    # 4. 运行异步主流程
    # ------------------------------------------------------------------
    asyncio.run(
        _run_async(
            config=config,
            data_store=data_store,
            dry_run=dry_run,
            verify_symbols=verify_symbols,
            run_stage_flags=run_stage_flags,
            run_stage_order=run_stage_order,
        )
    )


async def _run_async(
    config: Config,
    data_store: Optional[DataStore],
    dry_run: bool,
    verify_symbols: bool,
    run_stage_flags: dict,
    run_stage_order: list,
) -> None:
    """
    异步主流程：
    1. 初始化 LongbridgeBroker 并建立 WebSocket 连接
    2. --verify-symbols 模式：打印期权 symbol 样本后退出
    3. 正常模式：创建 PortfolioManager 并执行 manage()
    """
    broker = LongbridgeBroker(config, data_store=data_store)
    await broker.setup()

    try:
        if verify_symbols:
            await _verify_symbols(broker, config)
            return

        completion_future: Future[bool] = asyncio.get_event_loop().create_future()
        portfolio_manager = PortfolioManager(
            config=config,
            broker=broker,
            completion_future=completion_future,
            dry_run=dry_run,
            data_store=data_store,
            run_stage_flags=run_stage_flags,
            run_stage_order=run_stage_order,
        )

        await portfolio_manager.manage()
        await completion_future  # 等待 manage() 通过 set_result() 标记完成

    finally:
        await broker.teardown()


async def _verify_symbols(broker: LongbridgeBroker, config: Config) -> None:
    """
    打印期权链样本 symbol，用于验证 Longbridge API 返回的 symbol 格式。

    对配置中的第一个 portfolio symbol 执行：
    1. 获取最近 1 个到期日
    2. 打印该到期日的前 5 个行权价及对应 call/put symbol
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()

    symbols = list(config.portfolio.symbols.keys())
    if not symbols:
        console.print("[red]配置中没有 portfolio symbols，无法验证。[/red]")
        return

    symbol = symbols[0]
    console.print(f"\n[bold]验证 {symbol} 的期权 symbol 格式...[/bold]\n")

    try:
        expiry_dates = await broker.get_chain_expiry_dates(symbol)
        if not expiry_dates:
            console.print(f"[red]{symbol}: 没有找到到期日[/red]")
            return

        expiry = sorted(expiry_dates)[0]
        console.print(f"使用最近到期日: {expiry.strftime('%Y-%m-%d')}\n")

        strike_infos = await broker.get_chain_strikes_for_expiry(symbol, expiry)

        table = Table(title=f"{symbol} 期权 Symbol 样本")
        table.add_column("Strike")
        table.add_column("Call Symbol")
        table.add_column("Put Symbol")
        table.add_column("Standard")

        for si in strike_infos[:10]:
            table.add_row(
                str(si.price),
                si.call_symbol or "-",
                si.put_symbol or "-",
                "Y" if si.standard else "N",
            )

        console.print(table)
        console.print(
            "\n[green]Symbol 格式验证完成。"
            "请确认以上 symbol 可被 parse_option_symbol() 正确解析。[/green]"
        )

    except Exception as exc:
        console.print(f"[red]验证失败: {exc}[/red]")
