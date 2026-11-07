"""
增量绩效计算 —— 对齐平台 bigtradercpp/papertradingperf.py
========================================================

为什么是「增量」
----------------
模拟盘每天只跑当天一根 bar，手上没有完整净值序列，无法像回测那样一次算完
全部指标。平台的做法是：当日只算当日，历史结论从上一条绩效记录里往前推
（`max_portfolio_value`、`total_*` 累计量都存在库里）。本模块照同一算法实现。

口径依据
--------
逐行对照 `/opt/pyenv/.../bigtradercpp/papertradingperf.py`（明文 Python）：

  · init_capital        = 首条绩效的 portfolio_value / (1 + today_return)
                         —— 反推而非取配置值，因为首日已经跑过一天有收益了
  · cumulative_return   = (portfolio_value − init_capital) / init_capital
  · annualized_return   = (1+cum)^(252/n) − 1，n = 当前 index − 首条 index + 1
  · max_drawdown        = max(上一条的 max_drawdown, 当日回撤)  ← 滚动，只增不减
  · win_percent         = 累计盈利平仓笔数 / 累计平仓笔数
  · 近期收益            = 按**自然日**回溯（week 用 timedelta(weeks=1, days=-1)，
                         即 6 天，非笔误），取第一条 trading_day >= 目标日的记录，
                         收益率按 (cum_now − cum_then)/(1 + cum_then) 链式换算；
                         找不到基准记录时平台返回 −1（哨兵值，不是亏 100%）
  · risk_indicator      = alpha/beta/volatility/sharpe/ir，用「含当日」的全部
                         日收益序列现算（每天重算一遍，序列长度 = 已跑天数）

empyrical 依赖已剔除
--------------------
平台用 `empyrical.stats`。本模块按 empyrical 0.5.5 源码逐式重写为 numpy 实现，
只依赖 numpy/pandas，成果可整目录拷走。已核对的关键细节（差一点结果就不同）：

  · annual_volatility : std(ddof=1) × √252
  · sharpe_ratio      : mean(r − rf) / std(r − rf, ddof=1) × √252
  · beta_aligned      : cov/var 用 **ddof=0**（nanmean 口径），且
                        var < 1e-30 时置 NaN
  · alpha_aligned     : (mean(adj_r − β·adj_b) + 1)^252 − 1  ← 先年化再减 1，
                        不是简单 ×252
  · max_drawdown      : 由收益序列复利成净值曲线（起点 100）后取最小回撤
  · information_ratio : 平台**自实现**（未用 empyrical）：
                        mean(excess)×252 / (std(excess, ddof=1)×√252)
                        —— pandas .std() 默认 ddof=1，这里保持一致
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .constants import RISK_FREE_DAILY, YEAR_TRADING_DAYS


# ══════════════════════════════════════════════════════════════════
# empyrical 等价实现（numpy 版，逐式对照 empyrical 0.5.5 stats.py）
# ══════════════════════════════════════════════════════════════════

def annual_volatility(returns: Sequence[float],
                      ann_factor: int = YEAR_TRADING_DAYS) -> float:
    """年化波动率：std(ddof=1) × √252（empyrical.annual_volatility, alpha=2）。"""
    r = np.asarray(returns, dtype=float)
    if len(r) < 2:
        return float('nan')
    return float(np.nanstd(r, ddof=1) * math.sqrt(ann_factor))


def sharpe_ratio(returns: Sequence[float], risk_free: float = 0.0,
                 ann_factor: int = YEAR_TRADING_DAYS) -> float:
    """夏普：mean(超额) / std(超额, ddof=1) × √252（empyrical.sharpe_ratio）。"""
    r = np.asarray(returns, dtype=float)
    if len(r) < 2:
        return float('nan')
    adj = r - risk_free
    sd = np.nanstd(adj, ddof=1)
    if sd == 0:
        return float('nan')
    return float(np.nanmean(adj) / sd * math.sqrt(ann_factor))


def beta_aligned(returns: Sequence[float], factor_returns: Sequence[float],
                 risk_free: float = 0.0) -> float:
    """Beta = Cov(b, r)/Var(b)，**ddof=0**（empyrical 用 nanmean 实现）。

    empyrical 的做法：以基准序列为自变量做中心化，协方差/方差都取 nanmean，
    等价于 ddof=0；方差 < 1e-30 视为无效返回 NaN。
    """
    r = np.asarray(returns, dtype=float)
    b = np.asarray(factor_returns, dtype=float)
    if len(r) < 1 or len(b) < 2:
        return float('nan')
    # 与 empyrical 一致：returns 为 NaN 的位置，基准也置 NaN
    b = np.where(np.isnan(r), np.nan, b)
    resid = b - np.nanmean(b)
    cov = np.nanmean(resid * r)
    var = np.nanmean(resid * resid)
    if var < 1.0e-30:
        return float('nan')
    return float(cov / var)


def alpha_aligned(returns: Sequence[float], factor_returns: Sequence[float],
                  risk_free: float = 0.0, beta: Optional[float] = None,
                  ann_factor: int = YEAR_TRADING_DAYS) -> float:
    """Alpha（年化）：(mean(adj_r − β·adj_b) + 1)^252 − 1（empyrical.alpha_aligned）。"""
    r = np.asarray(returns, dtype=float)
    b = np.asarray(factor_returns, dtype=float)
    if len(r) < 2:
        return float('nan')
    if beta is None:
        beta = beta_aligned(r, b, risk_free)
    adj_r = r - risk_free
    adj_b = b - risk_free
    series = adj_r - beta * adj_b
    m = np.nanmean(series)
    try:
        return float(pow(m + 1.0, ann_factor) - 1.0)
    except OverflowError:
        return float('inf')


def alpha_beta_aligned(returns: Sequence[float], factor_returns: Sequence[float],
                       risk_free: float = 0.0,
                       ann_factor: int = YEAR_TRADING_DAYS):
    """返回 (alpha, beta)，与 empyrical.alpha_beta_aligned 同序。"""
    b = beta_aligned(returns, factor_returns, risk_free)
    a = alpha_aligned(returns, factor_returns, risk_free, beta=b,
                      ann_factor=ann_factor)
    return a, b


def max_drawdown_from_returns(returns: Sequence[float]) -> float:
    """由日收益序列复利成净值后取最大回撤（负值），对齐 empyrical.max_drawdown。"""
    r = np.asarray(returns, dtype=float)
    if len(r) < 1:
        return float('nan')
    r = np.where(np.isnan(r), 0.0, r)
    cum = np.empty(len(r) + 1, dtype=float)
    cum[0] = 100.0
    cum[1:] = 100.0 * np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(cum)
    return float(np.nanmin((cum - peak) / peak))


def information_ratio(returns: Sequence[float],
                      benchmark_returns: Sequence[float],
                      ann_factor: int = YEAR_TRADING_DAYS) -> float:
    """信息比率 —— 平台自实现口径（**不是** empyrical 的版本）：

        年化超额 = mean(excess) × 252
        跟踪误差 = std(excess, ddof=1) × √252     ← pandas .std() 默认 ddof=1
        IR = 年化超额 / 跟踪误差

    序列不足 2 期返回 NaN；跟踪误差为 0 返回 NaN（平台同）。
    """
    r = np.asarray(returns, dtype=float)
    b = np.asarray(benchmark_returns, dtype=float)
    if len(r) < 2:
        return float('nan')
    excess = r - b
    annualized_excess = np.nanmean(excess) * ann_factor
    tracking_error = np.nanstd(excess, ddof=1) * math.sqrt(ann_factor)
    if np.isnan(tracking_error):
        return 0.0
    if tracking_error == 0:
        return float('nan')
    return float(annualized_excess / tracking_error)


# ══════════════════════════════════════════════════════════════════
# 近期收益（自然日回溯）
# ══════════════════════════════════════════════════════════════════

# 平台 _get_prev_perf_by_date 的偏移表。week 用 (weeks=1, days=-1) = 6 天，
# 这是平台原样写法（非笔误），照抄以保持口径一致。
_TIMEPERIOD_OFFSETS = {
    'ten_day': pd.Timedelta(days=10),
    'week': pd.Timedelta(weeks=1, days=-1),
    'month': pd.Timedelta(days=30),
    'three_month': pd.Timedelta(days=90),
    'six_month': pd.Timedelta(days=180),
    'year': pd.Timedelta(days=365),
}

RECENT_PERIODS = ('ten_day', 'week', 'month', 'three_month', 'six_month', 'year')

# 平台在找不到基准绩效时填 -1 作哨兵（表示"区间不足，无意义"），
# 不是真的亏损 100%。展示层需按此哨兵值过滤。
RECENT_RETURN_SENTINEL = -1.0


def _prev_perf_by_date(all_performances: List[Dict[str, Any]],
                       timeperiod: str, trading_day: str) -> Optional[Dict[str, Any]]:
    """取 timeperiod 对应自然日之前最早的一条绩效（平台 _get_prev_perf_by_date）。

    平台逻辑：目标日 = 当日 − 偏移；顺序扫描历史，返回**第一条**
    trading_day >= 目标日 的记录（即区间左端点）。
    """
    if not all_performances:
        return None
    off = _TIMEPERIOD_OFFSETS.get(timeperiod)
    if off is None:
        return None
    target = (pd.Timestamp(trading_day) - off).strftime('%Y-%m-%d')
    for perf in all_performances:
        if str(perf['trading_day'])[:10] >= target:
            return perf
    return None


def evaluate_recent_returns(all_performances: List[Dict[str, Any]],
                            today_cumulative_return: float,
                            trading_day: str) -> Dict[str, float]:
    """近期收益率：把两个时点的累计收益率链式换算成区间收益率。

        区间收益 = (cum_now − cum_then) / (1 + cum_then)
    """
    out: Dict[str, float] = {}
    for tp in RECENT_PERIODS:
        perf = _prev_perf_by_date(all_performances, tp, trading_day)
        if perf is None:
            out[f'{tp}_return'] = RECENT_RETURN_SENTINEL
        else:
            then = float(perf['cumulative_return'])
            out[f'{tp}_return'] = (today_cumulative_return - then) / (1.0 + then)
    return out


# ══════════════════════════════════════════════════════════════════
# 风险指标
# ══════════════════════════════════════════════════════════════════

def evaluate_risk_indicator(today_return: float, today_benchmark_return: float,
                            all_performances: List[Dict[str, Any]],
                            risk_free_daily: float = RISK_FREE_DAILY,
                            year_trading_days: int = YEAR_TRADING_DAYS):
    """计算 alpha/beta/volatility/sharpe/ir 与超额收益最大回撤。

    序列 = 历史各日 today_return + 当日 today_return（含当日，每天重算）。
    基准协方差 |cov[1,1]| < 1e-30（基准几乎无波动）时全部指标留 0，对齐平台。

    返回 (risk_indicator: dict, alpha_max_drawdown: float)
    """
    strat: List[float] = []
    bench: List[float] = []
    alpha_returns: List[float] = []
    for perf in all_performances:
        bm = perf.get('benchmark') or {}
        strat.append(float(perf['today_return']))
        bench.append(float(bm.get('return', 0.0)))
        alpha_returns.append(float(bm.get('alpha_return', 0.0)))
    strat.append(float(today_return))
    bench.append(float(today_benchmark_return))
    alpha_returns.append(float(today_return) - float(today_benchmark_return))

    alpha = beta = volatility = sharpe = ir = 0.0
    cov = np.cov(np.vstack([strat, bench]), ddof=0)
    if abs(float(cov[1, 1])) >= 1.0e-30:
        alpha, beta = alpha_beta_aligned(strat, bench, risk_free_daily,
                                         ann_factor=year_trading_days)
        volatility = annual_volatility(strat, ann_factor=year_trading_days)
        if volatility != 0 and not np.isnan(volatility):
            sharpe = sharpe_ratio(strat, risk_free_daily,
                                  ann_factor=year_trading_days)
        ir = information_ratio(strat, bench, ann_factor=year_trading_days)

    def _clean(x: float) -> float:
        # 平台只对 alpha 做 isinf 归零；NaN 在此统一归零，避免 JSON 序列化出
        # NaN 字面量（SQLite/JSON 不接受），指标含义仍为"不可用"。
        v = float(x)
        return 0.0 if (math.isinf(v) or math.isnan(v)) else v

    risk_indicator = {
        'alpha': _clean(alpha),
        'beta': _clean(beta),
        'volatility': _clean(volatility),
        'sharpe': _clean(sharpe),
        'ir': _clean(ir),
    }
    amd = max_drawdown_from_returns(alpha_returns)
    return risk_indicator, (0.0 if math.isnan(amd) else float(amd))


# ══════════════════════════════════════════════════════════════════
# 当日绩效主入口（对齐 papertradingperf.evaluate）
# ══════════════════════════════════════════════════════════════════

def evaluate(*, day_raw: Dict[str, Any], trading_day: str,
             trading_day_index: int, capital_base: float,
             benchmark_instrument: str,
             all_performances: List[Dict[str, Any]],
             year_trading_days: int = YEAR_TRADING_DAYS,
             risk_free_daily: float = RISK_FREE_DAILY) -> Dict[str, Any]:
    """算出当日一条绩效记录。

    Parameters
    ----------
    day_raw : 当日单日原始数据（由 paper_engine 汇总），需含：
        portfolio_value      当日收盘总资产
        ending_cash          当日收盘现金
        long_value           多头持仓市值
        today_return         当日收益率（(今日总资产 − 昨日总资产)/昨日总资产；
                             首日为 (今日 − init_capital)/init_capital）
        today_sum_buy_value / today_sum_sell_value  当日买入/卖出金额
        commission           当日手续费
        realized_pnl         当日平仓盈亏
        profit_count / loss_count  当日盈利/亏损平仓笔数
        benchmark_close      基准当日收盘
        benchmark_returns    基准当日收益率
    trading_day : 'YYYY-MM-DD'
    trading_day_index : 该策略自首个交易日起的序号（首日 0）
    capital_base : 初始资金（仅在无历史绩效时用；有历史时按首条反推）
    all_performances : 历史绩效行（按 trading_day 升序，**不含当日**）

    Returns
    -------
    dict —— 可直接交给 StateStore.write_performance()
    """
    first_perf = all_performances[0] if all_performances else None
    prev_perf = all_performances[-1] if all_performances else None

    # 初始资金反推（平台口径）：首条绩效已含首日收益，故 init = pv/(1+r)。
    # 直接取 capital_base 会在首日有盈亏时让整条累计收益率序列错位。
    if first_perf:
        init_capital = float(first_perf['portfolio_value']) / (
            float(first_perf['today_return']) + 1.0)
        first_index = int(first_perf['trading_day_index'])
    else:
        init_capital = float(capital_base)
        first_index = int(trading_day_index)

    r: Dict[str, Any] = {
        'trading_day': trading_day,
        'trading_day_index': int(trading_day_index),
    }
    g = day_raw.get
    r['balance'] = float(g('ending_cash', 0.0))
    r['portfolio_value'] = float(g('portfolio_value', 0.0))
    r['total_market_value'] = float(g('long_value', 0.0)) - float(g('short_value', 0.0))
    r['today_sum_buy_value'] = float(g('today_sum_buy_value', 0.0))
    r['today_sum_sell_value'] = float(g('today_sum_sell_value', 0.0))
    r['today_commission'] = float(g('commission', 0.0))
    r['today_realized_pnl'] = float(g('realized_pnl', 0.0))

    # 累计量：上一条 + 当日
    def _prev(key: str, default: float = 0.0) -> float:
        return float(prev_perf[key]) if prev_perf else default

    r['total_buy_value'] = _prev('total_buy_value') + r['today_sum_buy_value']
    r['total_sell_value'] = _prev('total_sell_value') + r['today_sum_sell_value']
    r['total_commission'] = _prev('total_commission') + r['today_commission']

    profit_count = int(g('profit_count', 0))
    loss_count = int(g('loss_count', 0))
    r['total_close_count'] = int(_prev('total_close_count')) + profit_count + loss_count
    r['total_profit_count'] = int(_prev('total_profit_count')) + profit_count
    r['win_percent'] = round(
        r['total_profit_count'] / r['total_close_count']
        if r['total_close_count'] else 0.0, 6)

    # 盈亏比：平台按「当日平仓盈亏总额」的正负整体归入盈利额/亏损额
    # （不是逐笔分摊），累计后取均值之比。照原口径实现。
    realized = r['today_realized_pnl']
    r['total_profit_value'] = _prev('total_profit_value') + (
        realized if realized > 0 else 0.0)
    r['total_loss_value'] = _prev('total_loss_value') + (
        realized if realized <= 0 else 0.0)
    total_loss_count = r['total_close_count'] - r['total_profit_count']
    avg_loss = r['total_loss_value'] / total_loss_count if total_loss_count else 0.0
    avg_profit = (r['total_profit_value'] / r['total_profit_count']
                  if r['total_profit_count'] else 0.0)
    r['pnl_ratio'] = round(
        float(abs(avg_profit / avg_loss)) if avg_loss
        else (1.0 if r['total_profit_count'] else 0.0), 6)

    # 收益率
    r['today_return'] = float(g('today_return', 0.0))
    r['cumulative_return'] = (r['portfolio_value'] - init_capital) / init_capital

    # 年化：按已跑交易日数折算
    num_trading_days = int(trading_day_index) - first_index + 1
    if r['cumulative_return'] >= -1.0 and num_trading_days > 0:
        try:
            ann = pow(1.0 + r['cumulative_return'],
                      year_trading_days / num_trading_days) - 1.0
            if ann > 1e8:
                ann = 1e8
        except OverflowError:
            ann = 0.0
    else:
        ann = -1.0 if r['cumulative_return'] <= -1.0 else 0.0
    r['annualized_return'] = ann

    # 最大回撤：滚动更新（只增不减），历史峰值来自上一条记录
    if prev_perf:
        max_pv = float(prev_perf['max_portfolio_value'])
        prev_dd = float(prev_perf['max_drawdown'])
    else:
        max_pv = init_capital
        prev_dd = 0.0
    pv = r['portfolio_value']
    if max_pv > pv:
        current_dd = (max_pv - pv) / max_pv
    else:
        max_pv = pv
        current_dd = 0.0
    r['max_portfolio_value'] = max_pv
    r['max_drawdown'] = max(prev_dd, current_dd)

    # 基准与超额
    bm_close = float(g('benchmark_close', 0.0))
    bm_return = float(g('benchmark_returns', 0.0))
    if first_perf:
        first_bm_close = float((first_perf.get('benchmark') or {}).get(
            'close', bm_close) or bm_close)
    else:
        first_bm_close = bm_close
    bm_cum = ((bm_close - first_bm_close) / first_bm_close
              if first_bm_close else 0.0)
    benchmark = {
        'instrument': benchmark_instrument,
        'close': bm_close,
        'return': bm_return,
        'cum_return': bm_cum,
        'alpha_return': r['today_return'] - bm_return,
        'alpha_cum_return': r['cumulative_return'] - bm_cum,
    }
    risk_indicator, alpha_max_dd = evaluate_risk_indicator(
        r['today_return'], bm_return, all_performances,
        risk_free_daily=risk_free_daily, year_trading_days=year_trading_days)
    benchmark['alpha_max_drawdown'] = alpha_max_dd
    r['benchmark'] = benchmark
    r['risk_indicator'] = risk_indicator

    r.update(evaluate_recent_returns(all_performances, r['cumulative_return'],
                                     trading_day))
    return r
