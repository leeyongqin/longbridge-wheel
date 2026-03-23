"""
greeks.py — 期权 Greeks 计算与 ib_async 兼容适配层

职责：
1. FakeContract：鸭子类型替代 ib_async.Contract，供策略引擎使用
2. FakeGreeks：鸭子类型替代 ib_async Ticker.modelGreeks
3. FakeTicker：鸭子类型替代 ib_async.Ticker，封装 Longbridge 行情数据
4. build_fake_ticker()：将 LB calc_indexes / option_quote / depth 数据组装成 FakeTicker
5. bs_delta()：Black-Scholes delta（当 calc_indexes 返回 null 时的 fallback）

设计原则：
- 主路径：优先使用 Longbridge calc_indexes() 直接返回的 delta（SDK v3.0.3+）
- Fallback：calc_indexes 返回 null 时，使用 Black-Scholes 从 IV 计算 delta
- FakeTicker 完全兼容 util.py 中 midpoint_or_market_price()、would_increase_spread() 等函数
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from longbridge_wheel.compat import Contract, Option, Stock


# ---------------------------------------------------------------------------
# Black-Scholes Delta 计算（fallback）
# ---------------------------------------------------------------------------

def bs_delta(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    right: str,
) -> Optional[float]:
    """
    使用 Black-Scholes 公式计算欧式期权 delta。

    参数：
        S     : 标的价格（underlying price）
        K     : 行权价（strike price）
        T     : 到期年数（days_to_expiry / 365.0）
        r     : 无风险利率（如 0.045 = 4.5%）
        sigma : 隐含波动率（implied volatility，小数形式，如 0.25 = 25%）
        right : 'C'=看涨期权，'P'=看跌期权

    返回：
        delta 值（看涨期权 0~1，看跌期权 -1~0），计算失败时返回 None

    注意：
        - 美式期权用 B-S 公式是近似值，但对于 delta 目标选择已足够准确
        - T=0 时期权已到期，返回 None
        - sigma=0 时无法计算，返回 None
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None

    try:
        from scipy.stats import norm  # type: ignore[import]

        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        if right.upper().startswith("C"):
            return float(norm.cdf(d1))
        else:
            return float(norm.cdf(d1) - 1.0)
    except Exception:
        return None


def bs_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    right: str,
) -> Optional[float]:
    """
    使用 Black-Scholes 公式计算欧式期权理论价格。

    当 calc_indexes() 无 last_done（无行情订阅）时用作 price fallback，
    供 price_is_valid() / midpoint_or_market_price() 使用。
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        from scipy.stats import norm  # type: ignore[import]

        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        discount = math.exp(-r * T)
        if right.upper().startswith("C"):
            return float(S * norm.cdf(d1) - K * discount * norm.cdf(d2))
        else:
            return float(K * discount * norm.cdf(-d2) - S * norm.cdf(-d1))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# FakeContract — 鸭子类型替代 ib_async.Contract
# ---------------------------------------------------------------------------

@dataclass
class FakeContract(Contract):
    """
    鸭子类型替代 ib_async.Contract / Option。

    策略引擎通过以下属性访问合约信息：
        - symbol                         : 标的代码（如 "AAPL"）
        - secType                        : "STK" 或 "OPT"
        - localSymbol                    : LB 原始合约代码（如 "AAPL240119C00150000"）
        - strike                         : 行权价（float）
        - right                          : "C" 或 "P"
        - lastTradeDateOrContractMonth   : 到期日字符串 "YYYYMMDD"
        - exchange                       : 交易所（"SMART" 占位符）
        - currency                       : 货币（"USD"）
        - conId                          : LB 无 conId，设为 0
        - multiplier                     : 合约乘数，期权通常为 "100"
    """
    symbol: str                          # 标的代码，如 "AAPL"
    secType: str                         # "STK" 或 "OPT"
    localSymbol: str = ""                # LB 原始 symbol，如 "AAPL240119C00150000"
    strike: float = 0.0                  # 行权价（OPT 时有效）
    right: str = ""                      # "C" 或 "P"（OPT 时有效）
    lastTradeDateOrContractMonth: str = ""  # "YYYYMMDD" 或 "" (STK 时为空)
    exchange: str = "SMART"              # 占位符，LB 自动路由
    currency: str = "USD"
    conId: int = 0                       # LB 不需要 conId，保留兼容性
    multiplier: str = "100"             # 期权合约乘数
    primaryExch: str = ""                # 主要交易所（如 "NASDAQ", "NYSE"）

    # 内部使用，不暴露给策略引擎
    _underlying_price: float = field(default=0.0, repr=False)
    _dte: int = field(default=0, repr=False)

    @property
    def underlying_price(self) -> float:
        """标的当前价格（扫描期权链时设置）"""
        return self._underlying_price

    @underlying_price.setter
    def underlying_price(self, value: float) -> None:
        self._underlying_price = value

    @property
    def dte(self) -> int:
        """到期天数（从 lastTradeDateOrContractMonth 计算）"""
        return self._dte

    @dte.setter
    def dte(self, value: int) -> None:
        self._dte = value

    def is_option(self) -> bool:
        return self.secType == "OPT"

    def is_stock(self) -> bool:
        return self.secType == "STK"

    def lb_symbol(self) -> str:
        """
        返回 Longbridge API 使用的 symbol 字符串。

        股票："{TICKER}.US"，如 "AAPL.US"
        期权：直接使用 localSymbol（如 "AAPL240119C00150000"）
        """
        if self.secType == "STK":
            return f"{self.symbol}.US"
        return self.localSymbol


class FakeOption(FakeContract, Option):
    """
    期权合约的 FakeContract 子类，同时继承 compat.Option 标记类。

    isinstance(x, Option) → True，供策略引擎 isinstance 检查使用。
    所有数据字段来自 FakeContract（@dataclass）。
    """
    pass


class FakeStock(FakeContract, Stock):
    """
    股票合约的 FakeContract 子类，同时继承 compat.Stock 标记类。

    isinstance(x, Stock) → True，供策略引擎 isinstance 检查使用。
    """
    pass


def build_stock_contract(symbol: str, primary_exchange: str = "") -> FakeStock:
    """构建美股股票合约（返回 FakeStock，通过 isinstance(x, Stock) 检查）"""
    return FakeStock(
        symbol=symbol,
        secType="STK",
        localSymbol=f"{symbol}.US",
        exchange="SMART",
        currency="USD",
        primaryExch=primary_exchange,
    )


def parse_option_symbol(lb_symbol: str) -> Optional[FakeContract]:
    """
    解析 Longbridge 期权合约代码，构建 FakeContract。

    假设 OCC 格式：{TICKER}{YYMMDD}{C/P}{strike×1000 零填充到8位}
    示例："AAPL240119C00150000" → symbol=AAPL, expiry=20240119, right=C, strike=150.0

    ⚠️ 注意：LB 实际格式未经验证，首次使用请通过 --verify-symbols 确认。
    解析失败时返回 None（由调用方处理）。
    """
    if not lb_symbol:
        return None

    try:
        # LB 返回的 symbol 带市场后缀（如 "SPY260515P679000.US"）
        # 解析时去掉后缀，localSymbol 保留原始格式供 lb_symbol() 使用
        symbol_clean = lb_symbol.split(".")[0] if "." in lb_symbol else lb_symbol

        # 找到数字开头的位置（ticker 长度可变：SPY=3, AAPL=4, etc.）
        i = 0
        while i < len(symbol_clean) and not symbol_clean[i].isdigit():
            i += 1

        # 至少需要：ticker(≥1) + YYMMDD(6) + C/P(1) + strike(≥1)
        if i == 0 or i + 8 > len(symbol_clean):
            return None

        ticker = symbol_clean[:i]              # 如 "SPY", "AAPL"
        date_str = symbol_clean[i : i + 6]    # YYMMDD，如 "260515"
        right = symbol_clean[i + 6]            # "C" 或 "P"
        strike_str = symbol_clean[i + 7:]      # 剩余全部为行权价，如 "679000"

        if right not in ("C", "P"):
            return None
        if not strike_str or not strike_str.isdigit():
            return None

        # 将 YYMMDD 转换为 YYYYMMDD
        year = int(date_str[:2])
        year_full = 2000 + year if year < 80 else 1900 + year
        expiry = f"{year_full}{date_str[2:6]}"  # "20260515"

        strike = int(strike_str) / 1000.0  # 679000 → 679.0

        from longbridge_wheel.options import option_dte
        dte = option_dte(expiry)

        contract = FakeOption(
            symbol=ticker,
            secType="OPT",
            localSymbol=lb_symbol,
            strike=strike,
            right=right,
            lastTradeDateOrContractMonth=expiry,
            exchange="SMART",
            currency="USD",
            multiplier="100",
        )
        contract.dte = dte
        return contract

    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# FakeGreeks — 鸭子类型替代 ib_async Ticker.modelGreeks
# ---------------------------------------------------------------------------

@dataclass
class FakeGreeks:
    """
    鸭子类型替代 ib_async 的 OptionComputation（modelGreeks）。

    策略引擎通过 ticker.modelGreeks.delta、.gamma 等访问 greeks。
    所有字段均为 Optional，与 ib_async 行为一致（数据缺失时为 None）。
    """
    delta: Optional[float] = None    # delta：看涨 0~1，看跌 -1~0
    gamma: Optional[float] = None    # gamma
    theta: Optional[float] = None    # theta（每日时间价值衰减）
    vega: Optional[float] = None     # vega（对 IV 的敏感度）
    rho: Optional[float] = None      # rho（对利率的敏感度）
    optPrice: Optional[float] = None # 期权理论价格（用作 price fallback）
    impliedVol: Optional[float] = None  # 隐含波动率


# ---------------------------------------------------------------------------
# FakeTicker — 鸭子类型替代 ib_async.Ticker
# ---------------------------------------------------------------------------

class FakeTicker:
    """
    鸭子类型替代 ib_async.Ticker。

    策略引擎通过以下方法/属性访问 ticker 数据：
        - ticker.midpoint()          → bid/ask 中间价（或 last_done fallback）
        - ticker.marketPrice()       → 最新成交价
        - ticker.modelGreeks         → FakeGreeks 对象
        - ticker.callOpenInterest    → 看涨期权持仓量
        - ticker.putOpenInterest     → 看跌期权持仓量
        - ticker.contract            → FakeContract 对象

    util.py 中的 midpoint_or_market_price() 调用链：
        1. ticker.midpoint() 非 NaN → 返回 midpoint
        2. ticker.marketPrice() 非 NaN → 返回 marketPrice
        3. ticker.modelGreeks.optPrice → 返回 optPrice
        4. 返回 0.0
    """

    def __init__(
        self,
        contract: FakeContract,
        last: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        model_greeks: Optional[FakeGreeks] = None,
        call_open_interest: float = 0.0,
        put_open_interest: float = 0.0,
    ) -> None:
        self.contract = contract
        self._last = last
        self._bid = bid
        self._ask = ask
        self.modelGreeks = model_greeks
        self.callOpenInterest = call_open_interest
        self.putOpenInterest = put_open_interest

    def midpoint(self) -> float:
        """
        返回 bid/ask 中间价。

        有真实 bid/ask 时（通过 depth() 获取）返回中间价；
        否则退回到 last_done，使 midpoint_or_market_price() 正常工作。
        """
        if (
            self._bid is not None
            and self._ask is not None
            and self._bid > 0
            and self._ask > 0
        ):
            return (self._bid + self._ask) / 2.0
        # fallback：last_done 作为价格代理
        if self._last and self._last > 0:
            return self._last
        return float("nan")  # ib_async 约定：无效时返回 NaN

    def marketPrice(self) -> float:
        """返回最新成交价"""
        if self._last and self._last > 0:
            return self._last
        return float("nan")

    @property
    def bid(self) -> float:
        return self._bid if self._bid is not None else float("nan")

    @property
    def ask(self) -> float:
        return self._ask if self._ask is not None else float("nan")

    @property
    def last(self) -> float:
        return self._last if self._last else float("nan")

    def __repr__(self) -> str:
        return (
            f"FakeTicker(symbol={self.contract.symbol}, "
            f"last={self._last:.4f}, "
            f"bid={self._bid}, ask={self._ask}, "
            f"delta={self.modelGreeks.delta if self.modelGreeks else None})"
        )


# ---------------------------------------------------------------------------
# build_fake_ticker() — 从 LB 数据组装 FakeTicker
# ---------------------------------------------------------------------------

def build_fake_ticker(
    contract: FakeContract,
    last_done: Optional[float],
    delta: Optional[float],
    gamma: Optional[float] = None,
    theta: Optional[float] = None,
    vega: Optional[float] = None,
    rho: Optional[float] = None,
    implied_vol: Optional[float] = None,
    open_interest: Optional[float] = None,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    risk_free_rate: float = 0.045,
    hist_vol: Optional[float] = None,
) -> FakeTicker:
    """
    将 Longbridge API 返回的数据组装成 FakeTicker。

    数据来源：
        - last_done, delta, gamma...  : 来自 calc_indexes()
        - bid, ask                    : 来自 depth()（可选，若无则 midpoint() 退回 last_done）
        - implied_vol                 : 来自 calc_indexes() 或 option_quote()
        - hist_vol                    : 标的历史波动率（无行情订阅时用作 B-S fallback）

    delta 获取策略：
        1. 优先使用 calc_indexes() 返回的 delta
        2. 若 delta 为 None 且有 IV，使用 Black-Scholes(IV) 计算
        3. 若 IV 也无，且有 hist_vol，使用 Black-Scholes(hist_vol) 计算
        4. 若均失败，delta 保持 None（该合约在筛选时会被跳过）

    price 获取策略（last_done）：
        1. 优先使用 calc_indexes() 返回的 last_done
        2. 若 last_done 为 0 且有波动率，使用 B-S 理论价格估算
    """
    last = last_done or 0.0

    # 有效波动率：IV 优先，fallback 到历史波动率
    eff_vol = implied_vol or hist_vol

    # 尝试 Black-Scholes fallback delta
    computed_delta = delta
    if computed_delta is None and eff_vol and contract.is_option():
        computed_delta = bs_delta(
            S=contract.underlying_price,
            K=contract.strike,
            T=contract.dte / 365.0,
            r=risk_free_rate,
            sigma=eff_vol,
            right=contract.right,
        )

    # 无 last_done 时用 B-S 理论价格估算（仅当 hist_vol fallback 激活时）
    if last <= 0.0 and eff_vol and contract.is_option():
        theoretical = bs_price(
            S=contract.underlying_price,
            K=contract.strike,
            T=contract.dte / 365.0,
            r=risk_free_rate,
            sigma=eff_vol,
            right=contract.right,
        )
        if theoretical and theoretical > 0:
            last = theoretical

    greeks = FakeGreeks(
        delta=computed_delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        rho=rho,
        optPrice=last,          # 用 last_done 作为 optPrice（price fallback）
        impliedVol=implied_vol,
    )

    # 区分看涨和看跌的持仓量
    oi = float(open_interest) if open_interest is not None else 0.0
    is_call = contract.right.upper().startswith("C")

    return FakeTicker(
        contract=contract,
        last=last,
        bid=bid,
        ask=ask,
        model_greeks=greeks,
        call_open_interest=oi if is_call else 0.0,
        put_open_interest=oi if not is_call else 0.0,
    )


def build_stock_ticker(
    contract: FakeContract,
    last_done: float,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
) -> FakeTicker:
    """
    为股票（STK）构建简单 FakeTicker（无 greeks）。

    用于 OptionChainScanner 获取标的价格。
    """
    return FakeTicker(
        contract=contract,
        last=last_done,
        bid=bid,
        ask=ask,
        model_greeks=None,
        call_open_interest=0.0,
        put_open_interest=0.0,
    )


# ---------------------------------------------------------------------------
# 辅助函数：从 Decimal 安全转换为 float
# ---------------------------------------------------------------------------

def decimal_to_float(value: Optional[Decimal]) -> Optional[float]:
    """将 Longbridge SDK 返回的 Decimal 安全转换为 float，None 保持 None"""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
