"""
entry.py — CLI 入口重导出

pyproject.toml console_scripts 中注册的入口：
    longbridge-wheel = longbridge_wheel.entry:main
"""

from longbridge_wheel.main import cli


def main() -> None:
    cli()
