"""
main.py — Click CLI 命令定义

定义 `longbridge-wheel` 命令行入口，参数对应 pyproject.toml 中的 console_scripts。
"""

import logging

import click
import click_log

logger = logging.getLogger(__name__)
click_log.basic_config(logger)

CONTEXT_SETTINGS = dict(
    help_option_names=["-h", "--help"],
    auto_envvar_prefix="LONGBRIDGE_WHEEL",
)


@click.command(context_settings=CONTEXT_SETTINGS)
@click_log.simple_verbosity_option(logger, default="WARNING")
@click.option(
    "-c",
    "--config",
    help="Path to toml config file",
    required=True,
    default="thetagang.toml",
    type=click.Path(exists=True, readable=True),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Perform a dry run: display planned orders without placing real trades.",
)
@click.option(
    "--verify-symbols",
    is_flag=True,
    help="Print sample option symbols for one expiry and exit (verify OCC format).",
)
def cli(
    config: str,
    dry_run: bool,
    verify_symbols: bool,
) -> None:
    """Longbridge Wheel — 基于长桥证券 API 的期权 Wheel 策略自动交易机器人。

    使用 LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / LONGBRIDGE_ACCESS_TOKEN
    环境变量进行 API 认证。详见项目 CLAUDE.md。
    """
    # 降低 alembic 迁移日志级别（避免干扰终端输出）
    if logger.getEffectiveLevel() > logging.INFO:
        logging.getLogger("alembic").setLevel(logging.WARNING)
        logging.getLogger("alembic.runtime").setLevel(logging.WARNING)
        logging.getLogger("alembic.runtime.migration").setLevel(logging.WARNING)

    from .longbridge_wheel import start

    start(config, dry_run=dry_run, verify_symbols=verify_symbols)
