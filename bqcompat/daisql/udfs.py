"""非技术指标的辅助 UDF：滚动百分位排名、滚动相关、线性加权均值、截面相关。

这些用 SQL 窗口表达不出来（或表达会绕得没法读），统一做成 Python UDF，
入参是「截至当日的历史 LIST」（截面类则是当日整个横截面）。
"""
from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np


def _f(seq) -> np.ndarray:
    if seq is None:
        return np.array([], dtype='float64')
    return np.asarray([np.nan if v is None else v for v in seq], dtype='float64')


def _tail(a: np.ndarray, n: int) -> np.ndarray:
    if n and n > 0:
        a = a[-n:]
    return a[np.isfinite(a)]


def roll_rank_pct(seq: Sequence, n: int = 0) -> Optional[float]:
    """当前值在过去 n 行中的百分位（0~1）；n<=0 表示用全部历史。"""
    a = _f(seq)
    if a.size == 0 or not np.isfinite(a[-1]):
        return None
    window = _tail(a, int(n))
    if window.size == 0:
        return None
    cur = float(a[-1])
    less = float(np.sum(window < cur))
    equal = float(np.sum(window == cur))
    return (less + 0.5 * equal) / window.size


def roll_corr(y_seq: Sequence, x_seq: Sequence, n: int = 0) -> Optional[float]:
    """过去 n 行的相关系数（Pearson）。"""
    y, x = _f(y_seq), _f(x_seq)
    m = min(y.size, x.size)
    if m == 0:
        return None
    y, x = y[-m:], x[-m:]
    mask = np.isfinite(y) & np.isfinite(x)
    y, x = y[mask], x[mask]
    if n and n > 0:
        y, x = y[-int(n):], x[-int(n):]
    if y.size < 3:
        return None
    sy, sx = y.std(), x.std()
    if sy == 0 or sx == 0:
        return None
    return float(np.corrcoef(y, x)[0, 1])


def decay_linear(seq: Sequence, n: int = 0) -> Optional[float]:
    """线性加权均值：窗口内越靠近当日权重越大（权重 1..n）。"""
    a = _f(seq)
    window = _tail(a, int(n))
    if window.size == 0:
        return None
    w = np.arange(1, window.size + 1, dtype='float64')
    return float(np.dot(window, w) / w.sum())


def cs_corr(y_seq: Sequence, x_seq: Sequence) -> Optional[float]:
    """截面（同一天）两列相关系数。"""
    return roll_corr(y_seq, x_seq, 0)


# DuckDB 注册用：参数是 LIST<DOUBLE>（可含 NULL）
UDFS = [
    # (SQL 名, Python 函数, 参数类型, 返回类型)
    ('bq_roll_rank_pct', roll_rank_pct, ['DOUBLE[]', 'INTEGER'], 'DOUBLE'),
    ('bq_roll_corr', roll_corr, ['DOUBLE[]', 'DOUBLE[]', 'INTEGER'], 'DOUBLE'),
    ('bq_decay_linear', decay_linear, ['DOUBLE[]', 'INTEGER'], 'DOUBLE'),
    ('bq_cs_corr', cs_corr, ['DOUBLE[]', 'DOUBLE[]'], 'DOUBLE'),
]
