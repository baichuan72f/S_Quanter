"""本地 daiSQL 映射模块：把 BigQuant DAI SQL 的平台扩展函数翻译成本地实现
==========================================================================

## 为什么单独一个子包

平台 `dai.query(sql)` 的语法主体是 **DuckDB SQL**（操作符、标量函数、聚合、
列表函数都与 DuckDB 同名同义），平台在此之上加了自己的两类扩展函数：

* `m_*`：**时序**函数，沿每只标的按日期滚动（`m_lag` / `m_avg` / `m_ta_rsi` …）
* `c_*`：**截面**函数，同一天横截面计算（`c_pct_rank` / `c_zscore` …）

原先把翻译逻辑塞在 `bqcompat/sql_dialect.py` 一个文件里，用正则手写窗口表达式，
技术指标类（`m_ta_*`）只能报「不支持」。现在按职责拆开：

    parse.py     扫描 SQL、做括号平衡与参数切分（纯语法，不含语义）
    registry.py  函数注册表：名字 → 语义类别 + 构造器（加函数只改这里）
    seq.py       m_* 时序函数 → DuckDB 窗口表达式
    cross.py     c_* 截面函数 → DuckDB 窗口表达式
    ta.py        m_ta_* / 加权 / 相关类 → 基于 TA-Lib 的 Python UDF
    runtime.py   往 DuckDB 连接注册 UDF（幂等）
    selftest.py  与 pandas / TA-Lib 基准逐项对拍

## 语义来源与校验

平台文档《DAI SQL 函数列表》给出的是 DuckDB 原生函数清单（实测与本机 DuckDB
行为一致）；`m_*` / `c_*` 的具体口径以**语料里的真实调用形态**为准：

    m_ta_ema(close, 5)                                   → 时序 EMA
    m_ta_rsi(close, 14)                                  → 时序 RSI
    m_ta_macd_dif(close, fastperiod:=12, slowperiod:=26, signalperiod:=9)
    m_ta_kdj_k(high, low, close, fastk_period:=9, slowk_period:=3, slowd_period:=3)
    m_corr(close, volume, 20)                            → 滚动相关
    m_avg(close, 20) / c_pct_rank(close)                 → 滚动均值 / 截面百分位

每一项都有 `selftest.py` 的对拍用例（与 pandas rolling / talib 直接比数值），
不靠"看起来对"。

## 用法

    from bqcompat.daisql import translate
    sql2, used = translate("select m_avg(close, 5) from cn_fund_bar1d")

`dai_compat.py` 在执行前自动调用；手工用时记得先 `bqcompat.install()`（会装 UDF）。
"""
from __future__ import annotations

from .errors import DialectError
from .registry import FUNCS, SUPPORTED, UNSUPPORTED, describe

# 导入即完成注册（registry 是模块级副作用）
from . import seq, cross, ta, unsupported_funcs  # noqa: F401,E402
from .parse import translate  # noqa: E402

__all__ = ['translate', 'DialectError', 'FUNCS', 'SUPPORTED', 'UNSUPPORTED',
           'describe']
