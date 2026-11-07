"""`m_ta_*` 技术指标 + 时序辅助函数：基于 TA-Lib 的 Python UDF。

为什么走 UDF 而不是 SQL 窗口：EMA / RSI / MACD / KDJ 是**有状态递推**指标，
DuckDB 的窗口聚合无法表达。做法是把「截至当日的历史序列」作为 LIST 传给 Python
函数，在函数里用 TA-Lib 算，取最后一个值：

    m_ta_rsi(close, 14)
      → bq_ta_rsi(list(close) OVER (PARTITION BY instrument ORDER BY date), 14)

代价是每行都要重算一次序列（O(n²)），在日频 + 数千标的 + 数十行的量级下可接受；
TA-Lib 缺失时退回 numpy/pandas 等价实现（数值略有差异，selftest 会标出来）。
"""
from __future__ import annotations

import math
from typing import List, Optional

import numpy as np

from .errors import DialectError
from .registry import FuncSpec, register

try:                      # TA-Lib 可选
    import talib as _talib
except Exception:         # noqa: BLE001
    _talib = None

TA_OVER = ('PARTITION BY instrument ORDER BY date '
           'ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW')


def _int(value, func: str, default: Optional[int] = None) -> int:
    if value is None:
        if default is None:
            raise DialectError(f'{func} 缺少窗口/周期参数')
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise DialectError(
            f'{func} 的周期参数必须是整数字面量，实际是 {value!r}') from None


def _named(named, *keys, default=None):
    for k in keys:
        if k in named:
            return named[k]
    return default


# ── 数值实现（talib 优先，numpy 兜底）──────────────────────────────────────
def _arr(seq) -> np.ndarray:
    if seq is None:
        return np.array([], dtype='float64')
    return np.asarray([np.nan if v is None else v for v in seq], dtype='float64')


def _last(value) -> Optional[float]:
    if value is None:
        return None
    arr = np.asarray(value, dtype='float64')
    if arr.size == 0:
        return None
    v = arr[-1]
    return None if not np.isfinite(v) else float(v)


def ema_last(seq, period: int) -> Optional[float]:
    a = _arr(seq)
    if a.size < period:
        return None
    if _talib is not None:
        return _last(_talib.EMA(a, timeperiod=period))
    # 兜底：与 talib 相同的递推（首值用前 period 个的均值初始化）
    alpha = 2.0 / (period + 1.0)
    prev = float(np.nanmean(a[:period]))
    for x in a[period:]:
        prev = alpha * float(x) + (1 - alpha) * prev
    return prev


def rsi_last(seq, period: int) -> Optional[float]:
    a = _arr(seq)
    if a.size <= period:
        return None
    if _talib is not None:
        return _last(_talib.RSI(a, timeperiod=period))
    diff = np.diff(a)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    ag, al = gains[:period].mean(), losses[:period].mean()
    for g, l in zip(gains[period:], losses[period:]):
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return float(100 - 100 / (1 + rs))


def macd_last(seq, fast: int, slow: int, signal: int, which: int) -> Optional[float]:
    a = _arr(seq)
    if _talib is not None:
        # 预热期交给 TA-Lib 自己判定（历史不足时它返回 NaN，_last 会转成 None），
        # 这里不要自行设门槛，否则空值位置会和参照实现差几行。
        dif, dea, _hist = _talib.MACD(a, fastperiod=fast, slowperiod=slow,
                                      signalperiod=signal)
        return _last(dif if which == 1 else dea)
    if a.size < slow + signal:
        return None
    def _ema(x, n):
        alpha = 2.0 / (n + 1.0)
        out = np.empty_like(x)
        out[:n - 1] = np.nan
        if x.size >= n:
            prev = float(np.nanmean(x[:n]))
            out[n - 1] = prev
            for i in range(n, x.size):
                prev = alpha * float(x[i]) + (1 - alpha) * prev
                out[i] = prev
        return out
    dif = _ema(a, fast) - _ema(a, slow)
    dea = _ema(dif[~np.isnan(dif)], signal)
    if which == 1:
        return _last(dif)
    return _last(dea)


def kdj_last(high, low, close, fastk: int, slowk: int, slowd: int,
             which: int) -> Optional[float]:
    h, l, c = _arr(high), _arr(low), _arr(close)
    n = min(h.size, l.size, c.size)
    if n < fastk + max(slowk, slowd):
        return None
    h, l, c = h[-n:], l[-n:], c[-n:]
    if _talib is not None:
        k, d = _talib.STOCH(h, l, c, fastk_period=fastk, slowk_period=slowk,
                            slowk_matype=0, slowd_period=slowd, slowd_matype=0)
        kv, dv = _last(k), _last(d)
    else:
        llv = np.array([h[max(0, i - fastk + 1):i + 1].min() for i in range(n)])
        hhv = np.array([h[max(0, i - fastk + 1):i + 1].max() for i in range(n)])
        rsv = np.where(hhv - llv == 0, 50.0,
                       (c - llv) / np.where(hhv - llv == 0, 1, hhv - llv) * 100)
        kv = dv = None
        k_prev = d_prev = 50.0
        for v in rsv:
            k_prev = (v + (slowk - 1) * k_prev) / slowk
            d_prev = (k_prev + (slowd - 1) * d_prev) / slowd
        kv, dv = float(k_prev), float(d_prev)
    if kv is None:
        return None
    if which == 1:
        return kv
    if which == 2:
        return dv
    return 3.0 * kv - 2.0 * dv if dv is not None else None


# ── UDF 壳子（DuckDB 以 LIST 传入历史序列）────────────────────────────────
def udf_ema(seq, period):
    return ema_last(seq, int(period))


def udf_rsi(seq, period):
    return rsi_last(seq, int(period))


def udf_macd_dif(seq, fast=12, slow=26, signal=9):
    return macd_last(seq, int(fast), int(slow), int(signal), 1)


def udf_macd_dea(seq, fast=12, slow=26, signal=9):
    return macd_last(seq, int(fast), int(slow), int(signal), 2)


def udf_kdj_k(h, l, c, fastk=9, slowk=3, slowd=3):
    return kdj_last(h, l, c, int(fastk), int(slowk), int(slowd), 1)


def udf_kdj_d(h, l, c, fastk=9, slowk=3, slowd=3):
    return kdj_last(h, l, c, int(fastk), int(slowk), int(slowd), 2)


def udf_kdj_j(h, l, c, fastk=9, slowk=3, slowd=3):
    return kdj_last(h, l, c, int(fastk), int(slowk), int(slowd), 3)


# ── SQL 构造器 ────────────────────────────────────────────────────────────
def _ema(args, named):
    col = args[0]
    n = _int(args[1] if len(args) > 1 else _named(named, 'n', 'period', 'timeperiod'),
             'm_ta_ema')
    return f'bq_ta_ema(list({col}) OVER ({TA_OVER}), {n})'


def _rsi(args, named):
    col = args[0]
    n = _int(args[1] if len(args) > 1 else _named(named, 'n', 'period', 'timeperiod'),
             'm_ta_rsi')
    return f'bq_ta_rsi(list({col}) OVER ({TA_OVER}), {n})'


def _macd(which: str):
    def build(args, named):
        col = args[0]
        fast = _int(_named(named, 'fastperiod', 'fast'), 'm_ta_macd', 12)
        slow = _int(_named(named, 'slowperiod', 'slow'), 'm_ta_macd', 26)
        sig = _int(_named(named, 'signalperiod', 'signal'), 'm_ta_macd', 9)
        return (f'bq_ta_macd_{which}(list({col}) OVER ({TA_OVER}), '
                f'{fast}, {slow}, {sig})')
    return build


def _kdj(which: str):
    def build(args, named):
        if len(args) < 3:
            raise DialectError(
                f'm_ta_kdj_{which} 需要 (high, low, close) 三个位置参数，'
                f'实际 {len(args)} 个；本地按 TA-Lib STOCH 口径实现')
        h, l, c = args[0], args[1], args[2]
        fastk = _int(_named(named, 'fastk_period', 'fastk'), 'm_ta_kdj', 9)
        slowk = _int(_named(named, 'slowk_period', 'slowk'), 'm_ta_kdj', 3)
        slowd = _int(_named(named, 'slowd_period', 'slowd'), 'm_ta_kdj', 3)
        return (f'bq_ta_kdj_{which}(list({h}) OVER ({TA_OVER}), '
                f'list({l}) OVER ({TA_OVER}), list({c}) OVER ({TA_OVER}), '
                f'{fastk}, {slowk}, {slowd})')
    return build


register(FuncSpec('m_ta_ema', 'ta', _ema,
                  doc='时序 EMA（TA-Lib）', min_args=1, max_args=2))
register(FuncSpec('m_ta_rsi', 'ta', _rsi,
                  doc='时序 RSI（TA-Lib）', min_args=1, max_args=2))
register(FuncSpec('m_ta_macd_dif', 'ta', _macd('dif'),
                  doc='MACD 的 DIF 线（TA-Lib）', min_args=1, max_args=1))
register(FuncSpec('m_ta_macd_dea', 'ta', _macd('dea'),
                  doc='MACD 的 DEA 线（TA-Lib）', min_args=1, max_args=1))
register(FuncSpec('m_ta_kdj_k', 'ta', _kdj('k'),
                  doc='KDJ 的 K 值（TA-Lib STOCH）', min_args=3, max_args=3))
register(FuncSpec('m_ta_kdj_d', 'ta', _kdj('d'),
                  doc='KDJ 的 D 值（TA-Lib STOCH）', min_args=3, max_args=3))
register(FuncSpec('m_ta_kdj_j', 'ta', _kdj('j'),
                  doc='KDJ 的 J 值 = 3K-2D', min_args=3, max_args=3))
