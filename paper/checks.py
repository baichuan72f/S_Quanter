"""
数据完整性校验 —— 校验不过就中止，不允许降级继续跑
==================================================

为什么必须有这一关
------------------
数据延迟链条：平台 DAI 日线更新 → export_p0.py 拉取 → 本地任务跑。若任务在
数据更新完成前执行，会拿残缺数据算出错误信号（不报错但结果全错）。因此每日
跑批前必须校验，任一不过 → 抛 DataCheckError，当日不跑、不留快照。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import pandas as pd

from .local_data import LocalParquetDataSource, local_code


class DataCheckError(RuntimeError):
    """数据完整性校验未通过（当日中止，不留状态）。"""


def _pool_row_baseline(ds: LocalParquetDataSource, table: str,
                       trading_day: str, pool_local: List[str],
                       lookback: int = 20) -> Optional[float]:
    """近 lookback 个交易日「池内每日有行情行的标的数」均值（当日除外）。"""
    days = ds.get_trading_days('1990-01-01', trading_day)
    days = days[-lookback - 1:-1]
    if len(days) < 5:
        return None
    counts = []
    for d in days:
        df = ds._read_parts(table, d, d)
        if df.empty:
            counts.append(0)
        else:
            counts.append(int(df['instrument'].isin(pool_local).sum()))
    return float(pd.Series(counts).mean())


def check_data_ready(ds: LocalParquetDataSource, *,
                     trading_day: str,
                     instruments: List[str],
                     held_instruments: Optional[List[str]] = None,
                     min_row_ratio: float = 0.8) -> List[str]:
    """跑批前置校验。返回告警消息列表；致命问题直接抛 DataCheckError。

    校验项（方案文档 checks.py 必做三条）：
      1. 当日行情行数 vs 近 20 日均值（偏离超阈值 → 中止）
      2. 持仓标的与候选池标的当日是否有行情行（缺行 = 数据残缺 → 中止并列出；
         停牌标的有行但 volume=0，属正常，不算缺行）
      3. 当日行情 NaN 比例（超阈值 → 中止）
    """
    held_instruments = held_instruments or []
    warnings: List[str] = []

    funds, stocks = [], []
    for s in list(dict.fromkeys(list(instruments) + list(held_instruments))):
        (funds if str(s).split('.')[0][:2] in
         ('50', '51', '52', '56', '58', '15', '16', '18') else stocks).append(s)

    # 股票池与基金池分开校验（本地分区粒度不同）
    for table, insts in (('cn_stock_real_bar1d', stocks),
                         ('cn_fund_real_bar1d', funds)):
        if not insts:
            continue
        df_today = ds._read_parts(table, trading_day, trading_day)
        codes_today = {str(x) for x in df_today['instrument']} \
            if not df_today.empty else set()
        insts_local = [local_code(s) for s in insts]

        # ── 1. 行数基线：当日有行数 vs 近 20 日均值 ──
        n_today = sum(1 for c in insts_local if c in codes_today)
        baseline = _pool_row_baseline(ds, table, trading_day, insts_local)
        if baseline is not None and baseline > 0 \
                and n_today < baseline * min_row_ratio:
            missing = [s for s, lc in zip(insts, insts_local)
                       if lc not in codes_today]
            raise DataCheckError(
                f'{trading_day} {table} 行情行数异常：池内 {len(insts)} 只'
                f'仅 {n_today} 只有行（近 20 日均值 {baseline:.1f}）。'
                f'数据可能未更新完，缺行示例: {missing[:10]}')
        elif baseline is None and n_today == 0:
            raise DataCheckError(
                f'{trading_day} {table} 无任何行情行，数据未就绪，中止跑批。')

        # ── 2. 持仓标的是否缺行（候选池缺行已并入上面计数） ──
        for s in held_instruments:
            lc = local_code(s)
            table_of = ('cn_fund_real_bar1d'
                        if (lc.split('.')[0][:2] in
                            ('50', '51', '52', '56', '58', '15', '16', '18'))
                        else table)
            df_h = ds._read_parts(table_of, trading_day, trading_day,
                                  instruments=[lc])
            if df_h.empty:
                raise DataCheckError(
                    f'{trading_day} 持仓标的 {s} 当日无行情行 —— 数据残缺或'
                    f'代码错误。持仓市值无法重估，中止跑批（停牌标的有行、'
                    f'volume=0，不受影响）。')

        # ── 3. NaN 比例（当日 OHLC 大面积 NaN = 数据质量问题） ──
        if not df_today.empty:
            core = [c for c in ('open', 'close', 'pre_close')
                    if c in df_today.columns]
            pool = df_today[df_today['instrument'].isin(insts_local)]
            if not pool.empty and core:
                nan_ratio = float(pool[core].isna().all(axis=1).mean())
                if nan_ratio > 0.2:
                    raise DataCheckError(
                        f'{trading_day} {table} 当日 OHLC 全空行占比 '
                        f'{nan_ratio:.0%}（阈值 20%），数据质量问题，中止跑批。')
    return warnings
