# -*- coding: utf-8 -*-
"""
smalltrader_report — BigTrader 平台绩效报告渲染器（HTML 输出与平台 Performance 对齐）

本模块与回测引擎解耦，只依赖标准化数据对象 ReportData：
    1. smalltrader.Performance -> from_performance(perf)   （本地引擎）
    2. 平台 report json        -> from_platform_json(dict)（离线对拍）

渲染 HTML 与平台 notebook 输出逐块对齐（结构/CSS/表格/图表均复刻平台源码
bigquant/bigtrader/performance/_chartrender.py 与 _evaluator.py 的口径）。

关键口径（与平台实现一致，见 _evaluator.py）：
    - summary 指标      : empyrical 公式，日无风险利率 _RISK_FREE=0.0001173
    - 日收益序列         : 净值 pct_change 首日补 0（全长 n 点，如 163）
    - 年化收益率         : (1+累计)^(252/n) - 1
    - 收益报表（年/月度）: 段内 pv 首尾之比；段指标用段内 returns 列（含首日跨期收益）
    - 收益分布           : 滚动日历窗口 last("30D"/"90D"/…) 首尾之比
    - 区间收益图         : 日=逐日 returns；周/月/年=resample 后 (1+r) 累乘
    - 交易/持仓/日志明细 : 字段生成逻辑与 _evaluator.compute_transaction_details /
                           compute_positions_and_returns / _render_logs 一致
    - 相对收益率曲线     : 逐日 (策略日收益 - 基准日收益) 累乘（非简单差）
"""
from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ══════════════════════════════════════════════════════════════════
# CDN / 常量（与平台一致）
# ══════════════════════════════════════════════════════════════════
CDN_ECHARTS = 'https://cdn.bigquant.com/aistatic/aistudio/lib/echarts/assets/echarts.min'
CDN_ECHARTS_THEME = 'https://cdn.bigquant.com/aistatic/aistudio/lib/echarts/assets/themes/walden'
CDN_ITABLES_CSS = 'https://cdn.bigquant.com/s6/3rd/dt_for_itables/2.0.1/dt_bundle.css'
CDN_ITABLES_JS = 'https://cdn.bigquant.com/s6/3rd/dt_for_itables/2.0.1/dt_bundle.js'
CDN_DT_LANG = 'https://cdn.bigquant.com/s6/3rd/datatables/plug-ins/2.0.3/i18n/zh.json'

_RISK_FREE = float(os.getenv('RISK_FREE', '0.0001173'))   # 平台日无风险利率
_TRADING_DAYS = 252
# 平台图表主色板（_chartrender.COLORS）
COLORS = ['#E74C3C', '#5CA4FF', '#FFB301', '#DA8484', '#85A5FF']

# 鼠标滚轮调整时间滑块（dataZoom inside 缩放）开关：False=关闭（仅保留底部拖动滑块），
# True=开启（鼠标在图上滚动即可前后缩放时间段）。改这一行即可切换，无需改其它代码。
ENABLE_MOUSE_WHEEL_ZOOM = False

_DIV_CONTAINER = '<div style="width:100%; clear: both"class=chart-container id=__ID__>'

# ══════════════════════════════════════════════════════════════════
# 平台 CSS（原样提取；__CID__ 运行时替换为容器 id）
# ══════════════════════════════════════════════════════════════════
_CSS_TAB = """__CID__ .tab {
    overflow: hidden;
    margin-bottom: 10px;
    border-bottom: 1px solid #8888882E;
    display: flex;
    align-items: center;
}
@media screen and (max-width: 768px) { __CID__ .tab { justify-content: space-between; } }
@media screen and (min-width: 768px) { __CID__ .tab { justify-content: flex-start; gap: 30px; } }
__CID__ .title {
    margin: 0;
    font-size: 1.2em;
    padding: 10px 10px;
    margin-bottom: 10px;
    font-weight: bold;
}
__CID__ .tab button, __CID__ .tab span.title {
    background-color: inherit;
    border: none;
    outline: none;
    cursor: pointer;
    padding: 12px 16px;
    transition: 0.3s;
}
__CID__ .tab span.title { font-size: 1.3em; padding: 8px 16px; font-weight: bold; }
.vscode-dark __CID__ .tab button { color: rgba(255, 255, 255, 0.80); }
__CID__ .tab button:hover { color: #E74C3C; }
__CID__ .tab button.active { color: #E74C3C; border-bottom: 2px solid #e74c3c; }
__CID__ .legend_div { text-align: left; width: 120px; color: #757A88; font-size: 12px; }
.vscode-dark __CID__ .legend_div { color: rgba(255, 255, 255, 0.80); }
__CID__ .legend_span { border-radius: 100%; display: inline-block; width: 6px; height: 6px; margin-right: 5px; }
__CID__ .legend_bgc {
    border-radius: 5px;
    border: 1px solid rgba(0, 0, 0, 0.10);
    background: rgba(255, 255, 255, 0.80);
    box-shadow: 0px 10px 24px 0px rgba(117, 122, 136, 0.15);
    width: 145px;
    padding: 20px 18px 20px 20px;
}
.vscode-dark __CID__ .legend_bgc {
    border-radius: 5px;
    border: 1px solid #373737;
    background-color: rgba(37, 37, 38, 0.80);
    box-shadow: 0px 10px 24px 0px rgba(0, 0, 0, 0.19);
}
"""

_CSS_METRICS = """__CID__ .value {color: #E74C3C}
__CID__ .positive-True {color: #E74C3C}
__CID__ .positive-False {color: #24BB85}
__CID__ .bigtrader_performance_metrics {
    display: grid;
    padding-bottom: 10px;
    margin-bottom: 30px;
    grid-template-columns: repeat(11, 1fr);
    gap: 10px;
}
@media screen and (max-width: 768px) {
    __CID__ .bigtrader_performance_metrics { grid-template-columns: repeat(3, 1fr); }
}
@media screen and (max-width: 992px) and (min-width: 768px) {
    __CID__ .bigtrader_performance_metrics { grid-template-columns: repeat(4, 1fr); }
}
@media screen and (max-width: 1200px) and (min-width: 992px) {
    __CID__ .bigtrader_performance_metrics { grid-template-columns: repeat(6, 1fr); }
}
__CID__ .bigtrader_performance_metrics li {
    display: flex;
    height: 42px;
    flex-direction: column;
    list-style-type: none;
    text-align: center;
    padding: 0 10px;
}
__CID__ .bigtrader_performance_metrics .name {
    color: #888888;
    margin-top: 15px;
    margin-bottom: 11px;
    display: block;
    height: 17px;
    font-size: 13px;
}
.vscode-dark __CID__ ul li .name { color: rgba(255, 255, 255, 0.50); }
__CID__ ul .value {
    display: inline-block;
    height: auto;
    font-style: normal;
    font-weight: 550;
    line-height: normal;
    font-size: 16px;
}
"""

_CSS_EXTENDED = """__CID__ .extended_metrics {
    display: grid;
    padding-bottom: 10px;
    margin-bottom: 30px;
    grid-template-columns: repeat(9, 1fr);
    gap: 10px;
}
@media screen and (max-width: 768px) {
    __CID__ .extended_metrics { grid-template-columns: repeat(3, 1fr); }
}
@media screen and (max-width: 992px) and (min-width: 768px) {
    __CID__ .extended_metrics { grid-template-columns: repeat(4, 1fr); }
}
@media screen and (max-width: 1200px) and (min-width: 992px) {
    __CID__ .extended_metrics { grid-template-columns: repeat(5, 1fr); }
}
__CID__ .extended_metrics li {
    display: flex;
    height: 42px;
    flex-direction: column;
    list-style-type: none;
    text-align: center;
    padding: 0 10px;
}
__CID__ .extended_metrics .name {
    color: #888888;
    margin-top: 15px;
    margin-bottom: 11px;
    display: block;
    height: 17px;
    font-size: 13px;
}
.vscode-dark __CID__ .extended_metrics li .name { color: rgba(255, 255, 255, 0.50); }
__CID__ .extended_metrics .value {
    display: inline-block;
    height: auto;
    font-style: normal;
    font-weight: 550;
    line-height: normal;
    font-size: 16px;
    color: #333;
}
.vscode-dark __CID__ .extended_metrics .value { color: rgba(255, 255, 255, 0.85); }
"""

# 平台 tab 面板切换 JS（bigcharts 渲染结果，1:1 保留）
_JS_SHOWCHART = """var showChart=(a=>{let h=0,i=`tablinks`;var b=[];var c=[];
var d=document.getElementById(a).parentNode;
for(var e=h;e<d.children.length;e++){var f=d.children[e];
if(f.tagName==`DIV`&&(f.classList.contains(`chart-container`)||f.hasAttribute(`_echarts_instance_`)||f.id.length==32)){c.push(f);continue};
if(f.classList.contains(`tab`)){for(var g=h;g<f.children.length;g++){if(f.children[g].classList.contains(i)){b.push(f.children[g])}};continue}};
for(let d=h;d<c.length;d++){if(c[d].id==a){c[d].style.display=`block`;b[d].className=`tablinks active`}else{b[d].className=i;c[d].style.display=`none`}}});"""


# ══════════════════════════════════════════════════════════════════
# 标准化数据对象
# ══════════════════════════════════════════════════════════════════
@dataclass
class ReportData:
    """渲染所需全部数据（来源无关）。

    benchmark_values : 基准**累计收益率**曲线（0 起始），与 dates 等长；无基准 None
    leverage         : 每日总仓位（持仓市值/总资产，0~1），可选（主图副区）
    hold_count       : 每日持仓数量（只），可选（主图副区）
    """
    dates: List[str]
    portfolio_values: List[float]
    benchmark_values: Optional[List[float]] = None
    capital_base: float = 100000.0
    transactions: List[Dict] = field(default_factory=list)   # 平台展示字典行
    positions: List[Dict] = field(default_factory=list)
    logs: List[Dict] = field(default_factory=list)
    leverage: Optional[List[float]] = None
    hold_count: Optional[List[int]] = None
    start_date: str = ''
    end_date: str = ''
    meta: Dict = field(default_factory=dict)

    # ── 派生序列 ──────────────────────────────────────────────
    def pv_series(self) -> pd.Series:
        return pd.Series(self.portfolio_values, index=pd.to_datetime(self.dates))

    def returns(self) -> pd.Series:
        """日收益率（首日补 0，平台口径全长）"""
        return self.pv_series().pct_change().fillna(0.0)

    @property
    def has_benchmark(self) -> bool:
        return bool(self.benchmark_values and len(self.benchmark_values) == len(self.dates))

    def benchmark_returns(self) -> Optional[pd.Series]:
        """基准日收益率（累计曲线 -> 日收益，首日补 0）"""
        if not self.has_benchmark:
            return None
        s = pd.Series(self.benchmark_values, index=pd.to_datetime(self.dates))
        return (1.0 + s).pct_change().fillna(0.0)

    def relative_period_return(self) -> pd.Series:
        """相对收益率累计曲线（平台口径：逐日收益差累乘）"""
        alg_d = self.returns()
        bm_d = self.benchmark_returns()
        if bm_d is None:
            return pd.Series(0.0, index=alg_d.index)
        r_delta = (alg_d - bm_d).fillna(0.0)
        return (1.0 + r_delta).cumprod() - 1.0

    @staticmethod
    def from_platform_json(data: dict) -> 'ReportData':
        ec = data['equity_curve']
        acct = (data.get('accounts') or [{}])[0]
        positions = list(acct.get('positions') or [])
        rd = ReportData(
            dates=list(ec['dates']),
            portfolio_values=[float(x) for x in ec['portfolio_value']],
            benchmark_values=[float(x) for x in ec['benchmark_period_return']]
            if 'benchmark_period_return' in ec else None,
            capital_base=float((data.get('metadata') or {}).get('capital_base', 1e6)),
            transactions=list(acct.get('transactions') or []),
            positions=positions,
            logs=list(acct.get('logs') or []),
            start_date=(data.get('metadata') or {}).get('start_date', ''),
            end_date=(data.get('metadata') or {}).get('end_date', ''),
            meta=data,
        )
        # 从每日持仓表还原仓位/持仓数（主图副区数据；每日各股持仓占比之和≈总仓位）
        if positions and not rd.leverage:
            n = len(rd.dates)
            lev = [0.0] * n
            cnt = [0] * n
            by_date: Dict[str, List[Dict]] = {}
            for p in positions:
                by_date.setdefault(str(p.get('日期', '')), []).append(p)
            for i, d in enumerate(rd.dates):
                ps = by_date.get(d)
                if not ps:
                    continue
                cnt[i] = len(ps)
                for p in ps:
                    s = p.get('持仓占比')
                    if isinstance(s, str) and s.endswith('%'):
                        try:
                            lev[i] += float(s[:-1]) / 100.0
                        except ValueError:
                            pass
                    elif isinstance(s, (int, float)):
                        lev[i] += float(s)
            if any(v > 0 for v in lev):
                rd.leverage = lev
                rd.hold_count = cnt
        return rd


# ══════════════════════════════════════════════════════════════════
# 数值格式化（平台口径：round 到 2 位后 str，不补尾零）
# ══════════════════════════════════════════════════════════════════
def _fmt_pct2(x: float) -> str:
    return f'{round(x * 100, 2)}%'


def _num(v: Any, nd: int):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


def _new_id(prefix: str = '') -> str:
    return prefix + uuid.uuid4().hex


# ══════════════════════════════════════════════════════════════════
# summary 指标（直译平台 _evaluator.compute_summary + empyrical 公式）
# ══════════════════════════════════════════════════════════════════
def _nanmean(a):
    return float(np.nanmean(a))


def _sharpe_ratio(returns, risk_free=_RISK_FREE):
    """empyrical.sharpe_ratio: mean(r-rf)/std(r-rf, ddof=1) * sqrt(252)"""
    adj = np.asarray(returns, dtype=float) - risk_free
    if len(adj) < 2:
        return np.nan
    std = float(np.nanstd(adj, ddof=1))
    return float(np.nanmean(adj)) / std * np.sqrt(_TRADING_DAYS) if std > 0 else np.nan


def _annual_volatility(returns):
    if len(returns) < 2:
        return np.nan
    return float(np.nanstd(np.asarray(returns, dtype=float), ddof=1)) * np.sqrt(_TRADING_DAYS)


def _alpha_beta_aligned(returns, benchmark_returns):
    """empyrical.alpha_beta_aligned（日频、无 NaN 时的等价计算）

    beta  = mean(中心化(b) * r) / mean(中心化(b)^2)
    alpha = (1 + mean((r-rf) - beta*(b-rf))) ** 252 - 1
    """
    r = np.asarray(returns, dtype=float)
    b = np.asarray(benchmark_returns, dtype=float)
    if len(r) < 2:
        return np.nan, np.nan
    b_centered = b - _nanmean(b)
    beta = float(np.nanmean(b_centered * r) / max(np.nanmean(b_centered ** 2), 1e-30))
    adj_r = r - _RISK_FREE
    adj_b = b - _RISK_FREE
    alpha_series = adj_r - beta * adj_b
    alpha = float((1.0 + _nanmean(alpha_series)) ** _TRADING_DAYS - 1.0)
    return alpha, beta


def _information_ratio(returns, benchmark_returns):
    """平台 _evaluator._information_ratio 的 fallback 实现：
    mean(超额) * 252 / (std(超额) * sqrt(252))"""
    excess = np.asarray(returns, dtype=float) - np.asarray(benchmark_returns, dtype=float)
    if len(excess) < 2:
        return np.nan
    std = float(np.nanstd(excess, ddof=1))
    if np.isnan(std) or std == 0:
        return 0.0
    return float(np.nanmean(excess) * 252) / (std * np.sqrt(_TRADING_DAYS))


def _max_drawdowns(values: np.ndarray) -> Tuple[float, int, int]:
    running_max = np.maximum.accumulate(values)
    drawdowns = (values - running_max) / running_max
    trough_idx = int(np.argmin(drawdowns))
    peak_idx = int(np.argmax(values[: trough_idx + 1])) if trough_idx > 0 else 0
    return float(drawdowns[trough_idx]), peak_idx, trough_idx


def compute_summary(rd: ReportData) -> Dict[str, Any]:
    """summary 指标 dict（值即平台 summary：百分数已乘 100 round 2 等）"""
    pv = rd.pv_series()
    returns = rd.returns()
    n = len(pv)
    return_value = float(pv.iloc[-1] / pv.iloc[0] - 1)
    summary = {
        'return_ratio': round(return_value * 100, 2),
        'annual_return_ratio': round((pow(1 + return_value, _TRADING_DAYS / n) - 1) * 100, 2) if n > 0 else 'nan',
    }
    bm = rd.benchmark_returns()
    bm_total = float(rd.benchmark_values[-1]) if rd.has_benchmark else 0.0
    summary['benchmark_ratio'] = round(bm_total * 100, 2)

    alpha = beta = sharpe = ir = 0.0
    if bm is not None:
        alpha, beta = _alpha_beta_aligned(returns, bm)
        ir = _information_ratio(returns, bm)
    sharpe = _sharpe_ratio(returns)
    summary.update({
        'alpha': round(alpha, 2), 'beta': round(beta, 2),
        'sharp_ratio': round(sharpe, 2), 'ir': round(ir, 2),
        'return_volatility': round(_annual_volatility(returns) * 100, 2),
        'max_drawdown': round(abs(_max_drawdowns(pv.values)[0]) * 100, 2),
    })

    # 平仓统计（按交易明细中卖出普通成交的 realized_pnl；均值口径对齐平台 pnl_ratio）
    wins: List[float] = []
    losses: List[float] = []
    for t in rd.transactions:
        if t.get('买/卖') != '卖出' or t.get('成交类型') not in (None, '', '普通成交'):
            continue
        pnl = t.get('_realized_pnl')
        if pnl is None:
            txt = t.get('平仓盈亏')
            if not isinstance(txt, str) or '/' not in txt:
                continue
            try:
                pnl = float(txt.split('/')[0].replace(',', '').strip())
            except (ValueError, TypeError):
                continue
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(pnl)
    n_close = len(wins) + len(losses)
    summary['win_ratio'] = round(len(wins) / n_close * 100, 2) if n_close else 0.0
    if losses:
        ratio = float(np.mean(wins) / abs(np.mean(losses))) if wins else 0.0
    else:
        ratio = 1.0 if wins else 0.0
    summary['profit_loss_ratio'] = abs(round(ratio, 2))
    summary['_n_close'] = n_close
    return summary


# ══════════════════════════════════════════════════════════════════
# 扩展指标（直译平台 compute_extended_stats）
# ══════════════════════════════════════════════════════════════════
def compute_extended_stats(rd: ReportData, summary: Dict) -> Dict[str, Any]:
    returns = rd.returns()
    pv = rd.pv_series().values
    annual_return = summary.get('annual_return_ratio', 0)
    max_dd_pct = summary.get('max_drawdown', 0)
    calmar = round(annual_return / abs(max_dd_pct), 2) if max_dd_pct != 0 else 0.0
    annual_vol = round(_annual_volatility(returns) * 100, 2)
    neg = returns[returns < 0]
    downside = round(float(np.sqrt(np.mean(neg ** 2))) * math.sqrt(_TRADING_DAYS) * 100, 2) if len(neg) else 0.0
    max_dd, peak_idx, trough_idx = _max_drawdowns(pv)
    # 修复期：交易天数（index 步长）
    recovery = -1
    peak_value = pv[peak_idx]
    for i in range(trough_idx + 1, len(pv)):
        if pv[i] >= peak_value:
            recovery = i - trough_idx
            break
    recovery_str = f'{recovery}天' if recovery >= 0 else '未恢复'
    max_single_decline = round(float(returns.min()) * 100, 2)
    max_streak, cur = 0, 0
    for r in returns:
        if r < 0:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 0
    n_total = len(returns)
    n_loss = int((returns < 0).sum())
    loss_ratio = round(n_loss / n_total * 100, 2) if n_total > 0 else 0.0
    dates = pd.to_datetime(rd.dates)
    return {
        'annual_volatility': annual_vol,
        'calmar_ratio': calmar,
        'downside_risk': downside,
        'max_drawdown_recovery_period': recovery_str,
        'max_single_period_decline': max_single_decline,
        'max_consecutive_losses': max_streak,
        'loss_period_ratio': loss_ratio,
        'max_drawdown_start_date': dates[peak_idx].strftime('%Y-%m-%d'),
        'max_drawdown_end_date': dates[trough_idx].strftime('%Y-%m-%d'),
    }


# ══════════════════════════════════════════════════════════════════
# 区间收益（直译 compute_interval_returns）
# ══════════════════════════════════════════════════════════════════
def compute_interval_returns(rd: ReportData) -> Dict[str, List[Dict]]:
    raw = pd.DataFrame({'策略收益率': rd.returns(), '基准收益率': rd.benchmark_returns()})
    day = pd.DataFrame({
        'trading_day': pd.to_datetime(rd.dates).strftime('%Y-%m-%d'),
        '策略收益率': (raw['策略收益率'] * 100).round(2).values,
        '基准收益率': (raw['基准收益率'] * 100).round(2).values,
    })

    def _resample(freq):
        g = raw.resample(freq).agg(lambda s: float((1 + s).prod() - 1))
        g = g * 100
        g = g.round(2)
        g.index = g.index.strftime('%Y-%m-%d')
        g = g.reset_index()
        return g.rename(columns={'index': 'trading_day'})

    return {
        'day': day.to_dict('records'),
        'week': _resample('W').to_dict('records'),
        'month': _resample('ME').to_dict('records'),
        'year': _resample('YE').to_dict('records'),
    }


# ══════════════════════════════════════════════════════════════════
# 收益报表（直译 compute_return_report）
# ══════════════════════════════════════════════════════════════════
def _strategy_df(rd: ReportData) -> pd.DataFrame:
    bm = rd.benchmark_returns()
    return pd.DataFrame({
        'today_return': rd.returns().values,
        'benchmark_today_return': bm.values if bm is not None else np.zeros(len(rd.dates)),
        'portfolio_value': rd.portfolio_values,
        # 基准"close"列：用 (1+累计) 归一序列，仅参与 dd/收益之比（等效原价）
        'close': (1.0 + np.array(rd.benchmark_values, dtype=float)) if rd.has_benchmark else np.zeros(len(rd.dates)),
    }, index=pd.to_datetime(rd.dates))


def _period_metrics(sub: pd.DataFrame) -> Dict:
    if len(sub) < 2:
        return {'max_drawdown': 0.0, 'benchmark_max_drawdown': 0.0,
                'yearly_tracking_error': 0.0, 'information_ratio': 0.0,
                'volatility': 0.0, 'sharpe_ratio': 0.0}
    s_dd = _max_drawdowns(sub['portfolio_value'].values)[0]
    b_dd = _max_drawdowns(sub['close'].values)[0] if (sub['close'] > 0).all() else 0.0
    excess = sub['today_return'] - sub['benchmark_today_return']
    te = float(excess.std()) if len(excess) > 1 else 0.0
    yearly_te = te * np.sqrt(_TRADING_DAYS)
    mean_excess = float(excess.mean()) if len(excess) > 0 else 0.0
    ir = mean_excess / te if te != 0 else 0.0
    vol = float((sub['today_return'] - _RISK_FREE).std()) if len(sub) > 1 else 0.0
    sharpe = _sharpe_ratio(sub['today_return']) if len(sub) > 2 else 0.0
    return {'max_drawdown': s_dd, 'benchmark_max_drawdown': b_dd,
            'yearly_tracking_error': yearly_te, 'information_ratio': ir,
            'volatility': vol, 'sharpe_ratio': sharpe}


def _row_from_period_metrics(m: Dict[str, Any], label: Any, is_pct_label: bool = False) -> List:
    """单行统计表行：[标签, 策略收益, 基准收益, 策略回撤, 基准回撤, 跟踪误差,
    信息比率, 波动率, 夏普比率]（指标列格式与平台 _format_metrics_df 一致）"""
    cn2en = {
        '策略收益': '策略收益', '基准收益': '基准收益', '策略最大回撤': 'max_drawdown',
        '基准最大回撤': 'benchmark_max_drawdown', '跟踪误差': 'yearly_tracking_error',
        '信息比率': 'information_ratio', '波动率': 'volatility', '夏普比率': 'sharpe_ratio',
    }
    row = [str(label)]
    for col in ['策略收益', '基准收益', '策略最大回撤', '基准最大回撤', '跟踪误差',
                '信息比率', '波动率', '夏普比率']:
        v = m[cn2en[col]]
        if col in ('信息比率', '夏普比率'):
            row.append(round(v, 2))
        else:
            row.append(_fmt_pct2(v))
    return row


def compute_price_change_table(rd: ReportData) -> List[List]:
    """收益分布表（直译 _compute_price_change；last('ND') 用等价 mask 避免弃用告警）"""
    import datetime as _dt

    strategy = _strategy_df(rd)
    end = strategy.index[-1]
    now_year = end.year

    def _last_days(df: pd.DataFrame, days: int) -> pd.DataFrame:
        # pandas last('ND') 语义：含 end 在内最近 N 个日历日 → 起点 = end-(N-1)
        return df[df.index >= end - pd.Timedelta(days=days - 1)]

    def _period_change(sub):
        if sub.empty or len(sub) < 2:
            return 0.0, 0.0
        pv = sub['portfolio_value']
        return (pv.iloc[-1] - pv.iloc[0]) / pv.iloc[0], _max_drawdowns(pv.values)[0]

    periods = [
        ('运行以来', strategy),
        ('最近一月', _last_days(strategy, 30)),
        ('最近三月', _last_days(strategy, 90)),
        ('最近半年', _last_days(strategy, 180)),
        ('今年以来', strategy[strategy.index >= _dt.datetime(now_year, 1, 1)]),
        ('最近一年', _last_days(strategy, 365)),
        ('最近两年', _last_days(strategy, 730)),
        ('最近三年', _last_days(strategy, 1095)),
        ('最近五年', _last_days(strategy, 1825)),
    ]
    row_change = ['涨跌幅']
    row_dd = ['回撤']
    for label, sub in periods:
        c, d = _period_change(sub)
        row_change.append(_fmt_pct2(c))
        row_dd.append(_fmt_pct2(d))
    return [row_change, row_dd]


def compute_recent_yearly_rows(rd: ReportData) -> List[List]:
    """年-月度收益表（直译 _compute_recent_yearly，行=近1年…近N年）"""
    import datetime as _dt

    strategy = _strategy_df(rd)
    last_year = strategy.index[-1].year
    first_year = strategy.index[0].year
    rows = []
    for during_year in range(last_year - first_year + 1):
        sub = strategy[strategy.index >= _dt.datetime(last_year - during_year, 1, 1)]
        if sub.empty or len(sub) < 2:
            continue
        s_ret = (sub['portfolio_value'].iloc[-1] - sub['portfolio_value'].iloc[0]) / sub['portfolio_value'].iloc[0]
        b_ret = (sub['close'].iloc[-1] - sub['close'].iloc[0]) / sub['close'].iloc[0] if (sub['close'] > 0).all() else 0.0
        m = {'策略收益': s_ret, '基准收益': b_ret, **_period_metrics(sub)}
        rows.append(_row_from_period_metrics(m, f'近{during_year + 1}年'))
    return rows


def compute_yearly_rows(rd: ReportData) -> List[List]:
    strategy = _strategy_df(rd)
    rows = []
    for year_val, grp in strategy.groupby(strategy.index.year):
        if len(grp) < 2:
            continue
        s_ret = grp['portfolio_value'].iloc[-1] / grp['portfolio_value'].iloc[0] - 1
        b_ret = grp['close'].iloc[-1] / grp['close'].iloc[0] - 1 if (grp['close'] > 0).all() else 0.0
        m = {'策略收益': s_ret, '基准收益': b_ret, **_period_metrics(grp)}
        rows.append(_row_from_period_metrics(m, int(year_val)))
    rows.reverse()
    return rows


def compute_monthly_rows(rd: ReportData) -> List[List]:
    strategy = _strategy_df(rd)
    rows = []
    for period_end, grp in strategy.resample('ME'):
        if grp.empty or len(grp) < 2:
            continue
        s_ret = grp['portfolio_value'].iloc[-1] / grp['portfolio_value'].iloc[0] - 1
        b_ret = grp['close'].iloc[-1] / grp['close'].iloc[0] - 1 if (grp['close'] > 0).all() else 0.0
        m = {'策略收益': s_ret, '基准收益': b_ret, **_period_metrics(grp)}
        rows.append(_row_from_period_metrics(m, period_end.strftime('%Y-%m')))
    rows.reverse()
    return rows


# ══════════════════════════════════════════════════════════════════
# 交易/持仓/日志明细行（直译平台 compute_*_details）
# ══════════════════════════════════════════════════════════════════
def _pnl_info(realized_pnl: float, money: float) -> str:
    """平台 pnl_info：realized / (money-realized) 比例"""
    if money != realized_pnl:
        ratio = round(realized_pnl / (money - realized_pnl) * 100, 2)
    else:
        ratio = 0.0
    return f'{realized_pnl} / {ratio}%'


def transactions_rows(rd: ReportData) -> List[List]:
    """交易详情行（日期倒序）。rd.transactions 条目需含平台字段或本地 _ 字段。"""
    cols = []
    for t in rd.transactions:
        date = str(t.get('日期') or '')
        time_ = str(t.get('时间') or f'{date} 00:00:00.000')
        code = str(t.get('证券代码') or '')
        name = str(t.get('证券名称') or '')
        bs = t.get('买/卖') or ''
        amount = t.get('数量')
        price = _num(t.get('成交价'), 3)
        money = _num(t.get('成交金额'), 2)
        pnl = t.get('平仓盈亏')
        comm = _num(t.get('交易费用'), 2)
        kind = str(t.get('成交类型') or '')
        cols.append([date, time_, code, name, bs, abs(amount) if amount is not None else 0,
                     price if price is not None else '', money if money is not None else '',
                     pnl if pnl not in (None, '') else '--', comm if comm is not None else 0.0, kind])
    return cols


def positions_rows(rd: ReportData) -> List[List]:
    rows = []
    for p in rd.positions:
        rows.append([
            str(p.get('日期') or ''),
            str(p.get('证券代码') or ''),
            str(p.get('证券名称') or ''),
            _num(p.get('数量'), 0),
            _num(p.get('持仓均价'), 3),
            str(p.get('收盘价')) if p.get('收盘价') not in (None, '') else '',
            _num(p.get('持仓市值'), 2),
            str(p.get('持仓占比')) if p.get('持仓占比') not in (None, '') else '',
            str(p.get('收益')) if p.get('收益') not in (None, '') else '',
        ])
    return rows


def logs_rows(rd: ReportData) -> List[List]:
    rows = []
    for lg in rd.logs:
        date = lg.get('date') or ''
        if hasattr(date, 'strftime'):
            date = date.strftime('%Y-%m-%d')
        else:
            date = str(date)[:10]
        rows.append([date, str(lg.get('level') or 'INFO'), str(lg.get('msg') or '')])
    return rows


# ══════════════════════════════════════════════════════════════════
# HTML 块构造
# ══════════════════════════════════════════════════════════════════
def _css(s: str, cid: str) -> str:
    return s.replace('__CID__', f'#{cid}')


def _metrics_ul(cid: str, summary: Dict, has_benchmark: bool = True) -> str:
    """核心指标条（平台 PERFORMANCE_METRICS_HTML 直译；无基准时相关项显示 --）"""
    sharp = summary['sharp_ratio'] if summary['sharp_ratio'] > -1.0e6 else 'n/a'

    def item(name, value, cls_cond, attr=''):
        attr_h = f' name={attr}' if attr else ''
        cls = f'value positive-{bool(cls_cond)}'
        return (f'<li{attr_h}><span class=name>{name}</span>'
                f'<span class={cls}>{value}</span></li>')

    if has_benchmark:
        bm_items = (item('基准收益率', f"{summary['benchmark_ratio']}%",
                         summary['benchmark_ratio'] >= 0, '基准累计收益率')
                    + item('阿尔法', f"{summary['alpha']}", summary['alpha'] >= 0)
                    + f"<li><span class=name>贝塔</span><span class=value>{summary['beta']}</span></li>")
    else:
        bm_items = ('<li name=基准累计收益率><span class=name>基准收益率</span>'
                    '<span class=value>--</span></li>'
                    '<li><span class=name>阿尔法</span><span class=value>--</span></li>'
                    '<li><span class=name>贝塔</span><span class=value>--</span></li>')
    ul = (
        item('累计收益率', f"{summary['return_ratio']}%", summary['return_ratio'] >= 0)
        + item('年化收益率', f"{summary['annual_return_ratio']}%", summary['annual_return_ratio'] >= 0)
        + bm_items
        + item('夏普比率', f'{sharp}', summary['sharp_ratio'] >= 0)
        + f"<li><span class=name>胜率</span><span class=value>{summary['win_ratio']}%</span></li>"
        + f"<li><span class=name>盈亏比</span><span class=value>{summary['profit_loss_ratio']}</span></li>"
        + item('收益波动率', f"{summary['return_volatility']}%", summary['return_volatility'] >= 0, '年化收益波动率')
        + (item('信息比率', f"{summary['ir']}", summary['ir'] >= 0)
           if has_benchmark else '<li><span class=name>信息比率</span><span class=value>--</span></li>')
        + item('最大回撤', f"{summary['max_drawdown']}%", summary['max_drawdown'] >= 0)
    )
    return (f'<style>{_css(_CSS_METRICS, cid)}</style>'
            f'{_DIV_CONTAINER.replace("__ID__", cid)}<ul class=bigtrader_performance_metrics>{ul}</ul></div>')


def _extended_ul(cid: str, ext: Dict) -> str:
    items = [
        ('年化波动率', f"{ext['annual_volatility']}%"),
        ('卡玛比率', f"{ext['calmar_ratio']}"),
        ('下行风险', f"{ext['downside_risk']}%"),
        ('最大回撤修复期', ext['max_drawdown_recovery_period']),
        ('最大单期跌幅', f"{ext['max_single_period_decline']}%"),
        ('最大连跌期数', f"{ext['max_consecutive_losses']}"),
        ('亏损期占比', f"{ext['loss_period_ratio']}%"),
        ('最大回撤开始日期', ext['max_drawdown_start_date']),
        ('最大回撤结束日期', ext['max_drawdown_end_date']),
    ]
    ul = ''.join(f'<li><span class=name>{n}</span><span class=value>{v}</span></li>'
                 for n, v in items)
    return (f'<style>{_css(_CSS_EXTENDED, cid)}</style>'
            f'{_DIV_CONTAINER.replace("__ID__", cid)}<ul class=extended_metrics>{ul}</ul></div>')


def _tab_block(cid: str, title: str, buttons: List[Tuple[str, str]],
               default_id: str, panels_html: str) -> str:
    css = _css(_CSS_TAB, cid)
    btns = ''.join(f'<button class=tablinks onclick=showChart(\'{tid}\')>{txt}</button>'
                   for tid, txt in buttons)
    return (f'<style>{css}</style>{_DIV_CONTAINER.replace("__ID__", cid)}'
            f'<div class=title>{title}</div><div class=tab>{btns}</div>'
            f'{panels_html}<script>{_JS_SHOWCHART}showChart(`{default_id}`)</script></div>')


def _chart_pair(fig_id: str, option: dict, height: int = 500, after_init: str = '') -> str:
    opt_json = json.dumps(option, ensure_ascii=False)
    req = (f'<script>require.config({{paths:{{\'echarts\':`{CDN_ECHARTS}`,'
           f"'walden':`{CDN_ECHARTS_THEME}`}}}})</script>")
    init = (f'<script>require([`echarts`,`walden`],(a=>{{'
            f'let c=document.getElementById(`{fig_id}`);'
            f'let g={opt_json};'
            f'let k=a.init(c);k.setOption(g);'
            f'{after_init}'
            f'window.addEventListener(`resize`,()=>k.resize())}}))</script>')
    return f'{req}<div style="width:100%; height:{height}px;"id={fig_id}></div>{init}'


# 主收益图「窗口重定基」JS：拖动底部时间滑块（dataZoom）时，收益三线以窗口
# 首日重新归零——策略/基准/相对按 (1+r)/(1+r0)-1 复利重定基；回撤改为窗口内
# 回撤（窗口起点为 0）。仓位/数量是当日绝对值，不重定基。
# 依赖 series 带 id（alg/bm/rel/dd，见 main_chart_option）。
_JS_REBASE_MAIN = """
(function(){
  var byId={};(g.series||[]).forEach(function(s){if(s.id)byId[s.id]=s;});
  var alg=byId.alg&&byId.alg.data,bm=byId.bm&&byId.bm.data,
      rel=byId.rel&&byId.rel.data,dd=byId.dd&&byId.dd.data;
  if(!alg)return;
  var n=alg.length,busy=false;
  function pct(x){return Math.round(x*10000)/100;}
  function winStart(){
    var dzs=k.getOption().dataZoom||[],s=null,i,dz;
    for(i=0;i<dzs.length;i++){dz=dzs[i];if(dz.startValue!=null){s=dz.startValue;break;}}
    if(s==null){for(i=0;i<dzs.length;i++){dz=dzs[i];if(dz.start!=null){s=Math.round(dz.start/100*(n-1));break;}}}
    if(s==null)s=0;
    if(s<0)s=0; if(s>n-1)s=n-1;
    return s;
  }
  function rebase(){
    if(busy)return; busy=true;
    try{
      var s=winStart(),a0=1+alg[s]/100;
      var upd={series:[{id:'alg',data:alg.map(function(v){return pct((1+v/100)/a0-1);})}]};
      if(bm){var b0=1+bm[s]/100;
        upd.series.push({id:'bm',data:bm.map(function(v){return pct((1+v/100)/b0-1);})});}
      if(rel){var r0=1+rel[s]/100;
        upd.series.push({id:'rel',data:rel.map(function(v){return pct((1+v/100)/r0-1);})});}
      if(dd){
        var out=new Array(n),run=-Infinity;
        for(var t=0;t<n;t++){
          if(t<s){out[t]=null;continue;}
          var v=1+alg[t]/100; if(v>run)run=v;
          out[t]=pct(v/run-1);
        }
        upd.series.push({id:'dd',data:out});
      }
      k.setOption(upd);
    }finally{busy=false;}
  }
  k.on('datazoom',rebase);
  rebase();
})();
"""


def _itables_block(cid: str, columns: List[str], rows: List[List]) -> str:
    tbl_id = 'itables_' + uuid.uuid4().hex
    ths = ''.join(f'<th>{c}</th>' for c in columns)
    data_json = json.dumps(rows, ensure_ascii=False)
    pl = 20
    script = (f'<script type=module>import{{DataTable as a,jQuery as b}}from"{CDN_ITABLES_JS}";'
              f'document.querySelectorAll(`#{tbl_id}:not(.dataTable)`).forEach(b=>{{'
              f'const c3={data_json};'
              f'let c4={{"fnInfoCallback":``,"columnDefs":[{{"className":`dt-right`,"targets":`_all`}}],'
              f'"buttons":[{{"extend":`csvHtml5`,"text":`下载`,"title":`csv`,"bom":!0}}],'
              f'"layout":{{"topStart":`pageLength`,"topEnd":`search`,"bottomStart":`buttons`,"bottomEnd":`paging`}},'
              f'"pageLength":{pl},"language":{{"url":`{CDN_DT_LANG}`}},'
              f'"lengthMenu":[10,15,{pl},25,50,100,1000,-1],"order":[],"warn_on_dom":!0}};'
              f'c4[`data`]=c3;new a(b,c4)}})</script>')
    return (f'{_DIV_CONTAINER.replace("__ID__", cid)}'
            f'<table class="display nowrap" style="table-layout:auto; margin:auto; '
            f'caption-side:bottom; width:1" data-quarto-disable-processing=true id={tbl_id}>'
            f'<thead><tr style="text-align: right;">{ths}</tr></thead>'
            f'<tbody><tr><td>Loading...</td></tr></tbody></table>'
            f'<link href={CDN_ITABLES_CSS} rel=stylesheet>{script}</div>')


# ══════════════════════════════════════════════════════════════════
# 主收益图（平台 _render_returns_chart 直译：收益三线 + 仓位/数量副区）
# ══════════════════════════════════════════════════════════════════
def main_chart_option(rd: ReportData) -> dict:
    """主收益图（平台 _render_returns_chart 布局 + 增强）：

    上区：策略收益率(红+面积) / 基准收益率(蓝) / 相对收益率(黄, 默认隐藏)
    中区：回撤曲线（新增，红色填充，<=0）
    下区：仓位(%左轴) + 持仓数量(右轴)（数据存在时）
    底部：dataZoom 时间滑块（可拖动缩放时间段，与原版图下方滑块一致）；
         拖动时收益三线/回撤按窗口首日重定基（收益归零，见 _JS_REBASE_MAIN）
    """
    pv = rd.pv_series()
    dates = rd.dates
    n = len(dates)
    alg = [round(x * 100, 2) for x in (pv / pv.iloc[0] - 1).tolist()]
    dd = [round(x * 100, 2) for x in (pv / pv.cummax() - 1).tolist()]
    legend_data = ['策略收益率', '基准收益率', '相对收益率', '回撤']
    legend_selected = {'相对收益率': False}
    series = [
        {'id': 'alg', 'name': '策略收益率', 'type': 'line', 'smooth': True, 'showSymbol': False,
         'data': alg, 'color': COLORS[0], 'lineStyle': {'width': 2},
         'areaStyle': {'color': 'rgba(219, 51, 51, 0.12)'},
         'emphasis': {'focus': 'series'}},
    ]
    if rd.has_benchmark:
        bm_cum = np.array(rd.benchmark_values, dtype=float)
        rel_cum = rd.relative_period_return()
        series.append({'id': 'bm', 'name': '基准收益率', 'type': 'line', 'smooth': True,
                       'showSymbol': False,
                       'data': [round(x * 100, 2) for x in bm_cum], 'color': COLORS[1],
                       'lineStyle': {'width': 2}, 'emphasis': {'focus': 'series'}})
        series.append({'id': 'rel', 'name': '相对收益率', 'type': 'line', 'smooth': True,
                       'showSymbol': False,
                       'data': [round(x * 100, 2) for x in rel_cum], 'color': COLORS[2],
                       'lineStyle': {'width': 2}, 'emphasis': {'focus': 'series'}})
    else:
        legend_data = [d_ for d_ in legend_data if d_ != '基准收益率']
        legend_data = [d_ for d_ in legend_data if d_ != '相对收益率']
    # ── 中区：回撤（新增） ──
    series.append({'id': 'dd', 'name': '回撤', 'type': 'line', 'smooth': True, 'showSymbol': False,
                   'data': dd, 'color': 'rgba(231, 76, 60, 0.9)',
                   'lineStyle': {'width': 1},
                   'areaStyle': {'color': 'rgba(231, 76, 60, 0.22)'},
                   'yAxisIndex': 1, 'xAxisIndex': 1, 'z': 1})
    # 视觉布局（用户迭代要求）。总高 550px，网格均用像素固定"物理高度"：
    #   收益区  40~340  高 300px（不变）
    #   隔离带  340~365  25px（收益日期轴标签所在）
    #   回撤区  365~442  高 77px（吞并总高增量 50px + 滑块间距释放空间）
    #   仓位区  452~497  高 45px（不变）
    #   滑块    516~548  高 32px；仓位底 497 → 滑块顶 516 留 19px(≈5mm)
    GAP_ISOLATION = 25      # 收益底 ↔ 回撤顶
    H_RET = 300             # 收益曲线区物理高度（保持）
    H_DD = 77               # 回撤区物理高度（总高增量 + 间距增量全部给它）
    GAP_BM = 10             # 回撤底 ↔ 仓位顶
    H_POS = 45              # 仓位区物理高度（保持）
    GAP_SLIDER = 19         # 仓位底 ↔ 滑块顶（≈5mm）
    H_SLIDER = 32
    TOP_DD = 40 + H_RET + GAP_ISOLATION            # 365
    TOP_POS = TOP_DD + H_DD + GAP_BM               # 452
    grids = [
        {'left': 64, 'right': 104, 'top': 40, 'height': H_RET},
        {'left': 64, 'right': 104, 'top': TOP_DD, 'height': H_DD},
    ]
    x_axes = [
        {'type': 'category', 'data': dates, 'boundaryGap': False,
         'gridIndex': 0, 'axisLabel': {'hideOverlap': True}},
        {'type': 'category', 'data': dates, 'boundaryGap': False,
         'gridIndex': 1, 'axisLabel': {'show': False}},
    ]
    y_axes = [
        {'type': 'value', 'scale': True, 'axisLabel': {'formatter': '{value}%'}},
        {'type': 'value', 'scale': True, 'gridIndex': 1,
         'axisLabel': {'formatter': '{value}%'}},
    ]
    if rd.leverage is not None:
        # 下区：仓位 + 数量（平台副区；右轴给数量）
        grids.append({'left': 64, 'right': 104, 'top': TOP_POS, 'height': H_POS})
        x_axes.append({'type': 'category', 'data': dates, 'boundaryGap': False,
                       'gridIndex': 2, 'axisLabel': {'show': False}})
        y_axes.append({'type': 'value', 'scale': True, 'gridIndex': 2,
                       'axisLabel': {'formatter': '{value}%'}})
        y_axes.append({'type': 'value', 'scale': True, 'gridIndex': 2,
                       'position': 'right'})
        series.append({'id': 'lev', 'name': '仓位', 'type': 'line', 'smooth': True,
                       'showSymbol': False,
                       'data': [round(x * 100, 2) for x in rd.leverage],
                       'color': COLORS[3], 'yAxisIndex': 2, 'xAxisIndex': 2,
                       'lineStyle': {'width': 1.4}})
        series.append({'id': 'cnt', 'name': '数量', 'type': 'line', 'smooth': True,
                       'showSymbol': False,
                       'data': [int(x) for x in rd.hold_count or []],
                       'color': COLORS[4], 'yAxisIndex': 3, 'xAxisIndex': 2,
                       'lineStyle': {'width': 1.4}})
        # 底部时间滑块：距仓位底 19px(≈5mm)、高 32px
        datazoom = [
            {'type': 'slider', 'xAxisIndex': [0, 1, 2], 'bottom': 2,
             'height': H_SLIDER, 'startValue': 0, 'endValue': n - 1,
             'brushSelect': False, 'zoomLock': False},
        ]
        if ENABLE_MOUSE_WHEEL_ZOOM:
            datazoom.append({'type': 'inside', 'xAxisIndex': [0, 1, 2]})
    else:
        # 无仓位数据：仓位区域并入回撤（仍与滑块保留 19px 间距）
        grids[1]['height'] = (TOP_POS + H_POS) - TOP_DD
        datazoom = [
            {'type': 'slider', 'xAxisIndex': [0, 1], 'bottom': 2,
             'height': H_SLIDER, 'startValue': 0, 'endValue': n - 1,
             'brushSelect': False, 'zoomLock': False},
        ]
        if ENABLE_MOUSE_WHEEL_ZOOM:
            datazoom.append({'type': 'inside', 'xAxisIndex': [0, 1]})
    option = {
        'tooltip': {'trigger': 'axis', 'axisPointer': {'type': 'cross'}},
        'legend': {'data': legend_data, 'top': 8, 'selected': legend_selected},
        'grid': grids,
        'xAxis': x_axes,
        'yAxis': y_axes,
        'dataZoom': datazoom,
        'series': series,
    }
    return option


def interval_chart_option(rows: List[Dict]) -> dict:
    """区间收益图（平台 Bar：策略收益率 + 基准收益率 两根柱系列）。

    与平台实际页面一致（勿再改）：平台 notebook 报告 init 不带主题（echarts
    默认外观），option 显式声明 pyecharts 默认 9 色板，柱色按系列序取
    → 策略=#91cc75（草绿，色板第 2 色）、基准=#fac858（黄，第 3 色），
    已在原版 notebook HTML 逐值核实并由用户 hover 确认。
    （平台该图另有一个 trading_day 空占位系列占据色板第 1 色与 legend 首项，
    本地以可见两根柱直译，配色不变。）

    底部时间滑块（dataZoom）：参数逐字直译平台原版 notebook option
    （show/showDetail/showDataShadow/realtime/start/end/orient/
    xAxisIndex/zoomLock/top:'85%'/filterMode），拖拽可缩放时间段。"""
    data_s = [{'value': r.get('策略收益率', 0)} for r in rows]
    data_b = [{'value': r.get('基准收益率', 0)} for r in rows]
    return {
        'tooltip': {'trigger': 'axis'},
        'grid': {'left': 56, 'right': 16, 'top': 30, 'bottom': 36},
        'legend': {'data': ['策略收益率', '基准收益率'], 'top': 4},
        'xAxis': {'type': 'category', 'data': [r.get('trading_day', '') for r in rows],
                  'axisLabel': {'hideOverlap': True}},
        'yAxis': {'type': 'value'},
        'dataZoom': [
            # 平台同款滑块：start/end 0~100 显示全量，用户拖拽两端手柄缩放
            {'show': True, 'type': 'slider', 'showDetail': True,
             'showDataShadow': True, 'realtime': True, 'start': 0, 'end': 100,
             'orient': 'horizontal', 'xAxisIndex': [0, 1], 'zoomLock': False,
             'top': '85%', 'filterMode': 'filter'},
        ],
        'series': [
            {'name': '策略收益率', 'type': 'bar', 'data': data_s, 'color': '#91cc75',
             'barMaxWidth': 20},
            {'name': '基准收益率', 'type': 'bar', 'data': data_b, 'color': '#fac858',
             'barMaxWidth': 20},
        ],
    }


# ══════════════════════════════════════════════════════════════════
# 整页组装（平台页面结构 1:1）
# ══════════════════════════════════════════════════════════════════
def build_dashboard_html(rd: ReportData) -> str:
    summary = compute_summary(rd)
    ext = compute_extended_stats(rd, summary)
    outer_id = _new_id('bigchart')
    tabs_id = _new_id('bigchart')

    overview_id = _new_id('bigchart')
    trades_id = _new_id('bigchart')
    positions_id = _new_id('bigchart')
    logs_id = _new_id('bigchart')

    # ── 收益概况面板：指标条 + 收益曲线图 + 扩展指标 ────────────────
    overview_html = (f'{_DIV_CONTAINER.replace("__ID__", overview_id)}'
                     f'{_metrics_ul(_new_id("bigchart"), summary, rd.has_benchmark)}'
                     f'{_chart_pair(_new_id(), main_chart_option(rd), height=550, after_init=_JS_REBASE_MAIN)}'
                     f'{_extended_ul(_new_id("bigchart"), ext)}</div>')

    trades_html = _itables_block(
        trades_id,
        ['日期', '时间', '证券代码', '证券名称', '买/卖', '数量', '成交价',
         '成交金额', '平仓盈亏', '交易费用', '成交类型'],
        transactions_rows(rd))
    positions_html = _itables_block(
        positions_id,
        ['日期', '证券代码', '证券名称', '数量', '持仓均价', '收盘价',
         '持仓市值', '持仓占比', '收益'],
        positions_rows(rd))
    logs_html = _itables_block(logs_id, ['date', 'level', 'msg'], logs_rows(rd))

    main_tabs = _tab_block(
        tabs_id, 'SmallQuant(高性能回测&交易)',
        [(overview_id, '收益概况'), (trades_id, '交易详情'),
         (positions_id, '每日持仓和收益'), (logs_id, '日志')],
        overview_id,
        overview_html + trades_html + positions_html + logs_html)

    # ── 区间收益（日/周/月/年） ────────────────────────────────────
    intv_id = _new_id('bigchart')
    interval = compute_interval_returns(rd)
    fig_ids = {}
    panels = ''
    order = [('日收益', 'day'), ('周收益', 'week'), ('月收益', 'month'), ('年收益', 'year')]
    for label, key in order:
        records = interval[key]
        if not records:
            continue
        fid = _new_id()
        fig_ids[label] = fid
        panels += _chart_pair(fid, interval_chart_option(records))
    interval_tabs = _tab_block(intv_id, '区间收益',
                               [(fig_ids[l], l) for l, _ in order if l in fig_ids],
                               fig_ids['日收益'], panels)

    # ── 收益报表（收益分布/年-月度/年度/月度） ───────────────────────
    rep_id = _new_id('bigchart')
    t_ids = {k: _new_id('bigchart') for k in ['收益分布', '年/月度收益', '年度收益', '月度收益']}
    dist_cols = ['本策略', '运行以来', '最近一月', '最近三月', '最近半年', '今年以来',
                 '最近一年', '最近两年', '最近三年', '最近五年']
    stat_cols = ['最近周期', '策略收益', '基准收益', '策略最大回撤', '基准最大回撤',
                 '跟踪误差', '信息比率', '波动率', '夏普比率']
    year_cols = ['年份'] + stat_cols[1:]
    month_cols = ['月份'] + stat_cols[1:]
    report_panels = (
        _itables_block(t_ids['收益分布'], dist_cols, compute_price_change_table(rd))
        + _itables_block(t_ids['年/月度收益'], stat_cols, compute_recent_yearly_rows(rd))
        + _itables_block(t_ids['年度收益'], year_cols, compute_yearly_rows(rd))
        + _itables_block(t_ids['月度收益'], month_cols, compute_monthly_rows(rd)))
    report_tabs = _tab_block(
        rep_id, '收益报表',
        [(t_ids[k], k) for k in ['收益分布', '年/月度收益', '年度收益', '月度收益']],
        t_ids['收益分布'], report_panels)

    return (f'<style></style>{_DIV_CONTAINER.replace("__ID__", outer_id)}'
            f'{main_tabs}{interval_tabs}{report_tabs}</div>')


# ══════════════════════════════════════════════════════════════════
# 展示入口 / report 文本
# ══════════════════════════════════════════════════════════════════
def render_dashboard(rd: ReportData, display: bool = True) -> Optional[str]:
    html = build_dashboard_html(rd)
    if display:
        try:
            from IPython import get_ipython
            from IPython.display import HTML as _HTML, display as _display
            if get_ipython() is not None:
                _display(_HTML(html))
                return None
        except Exception as e:  # noqa: BLE001 - display 失败时降级为文本报告
            import sys
            import traceback
            sys.stderr.write(f'[smalltrader] HTML dashboard 展示失败，降级为文本报告: {e}\n')
            sys.stderr.write(traceback.format_exc())
    return html


def report_text_markdown(rd: ReportData) -> str:
    """平台 _report._print_text_summary 直译（run(render='report') 时打印）"""
    summary = compute_summary(rd)
    lines = ['## SmallTrader 回测报告', '', '### 绩效摘要', '']
    metrics = [
        ('累计收益率', f"{summary['return_ratio']}%"),
        ('年化收益率', f"{summary['annual_return_ratio']}%"),
        ('基准收益率', f"{summary['benchmark_ratio']}%"),
        ('阿尔法', f"{summary['alpha']}"),
        ('贝塔', f"{summary['beta']}"),
        ('夏普比率', f"{summary['sharp_ratio']}" if summary['sharp_ratio'] > -1.0e6 else 'n/a'),
        ('胜率', f"{summary['win_ratio']}%"),
        ('盈亏比', f"{summary['profit_loss_ratio']}"),
        ('收益波动率', f"{summary['return_volatility']}%"),
        ('信息比率', f"{summary['ir']}"),
        ('最大回撤', f"{summary['max_drawdown']}%"),
    ]
    name_width = max(len(m[0]) for m in metrics)
    lines.append(f"| {'指标':<{name_width}} | 值 |")
    lines.append(f"|{'-' * (name_width + 2)}|------|")
    for name, value in metrics:
        lines.append(f'| {name:<{name_width}} | {value} |')
    lines.append('')
    # ── 收益走势（平台取样规则：跨期 >=90 自然日按每月末日取样） ──
    lines += ['### 收益走势', '']
    pv = rd.pv_series()
    alg_cum = pv / pv.iloc[0] - 1
    bm_cum = pd.Series(rd.benchmark_values, index=pv.index) if rd.has_benchmark else None
    rel_cum = rd.relative_period_return()
    dates = pv.index
    n_days = len(pv)
    total_calendar_days = (dates[-1] - dates[0]).days
    sample_set = {0, n_days - 1}
    if n_days > 1:
        if total_calendar_days < 90:
            weeks = dates.isocalendar().week.values * 100 + dates.isocalendar().year.values
            for i in range(1, n_days):
                if i == n_days - 1 or (i + 1 < n_days and weeks[i] != weeks[i + 1]):
                    sample_set.add(i)
        else:
            months = dates.month.values
            for i in range(1, n_days):
                if i == n_days - 1 or (i + 1 < n_days and months[i] != months[i + 1]):
                    sample_set.add(i)
    lines.append('| 日期 | 策略收益率 | 基准收益率 | 相对收益率 |')
    lines.append('|------|----------|----------|----------|')
    for i in sorted(sample_set):
        d = dates[i].strftime('%Y-%m-%d')
        a = round(float(alg_cum.iloc[i]) * 100, 2)
        b = round(float(bm_cum.iloc[i]) * 100, 2) if bm_cum is not None else None
        r = round(float(rel_cum.iloc[i]) * 100, 2)
        if b is None:
            lines.append(f'| {d} | {a}% | -- | -- |')
        else:
            lines.append(f'| {d} | {a}% | {b}% | {r}% |')
    lines.append('')
    # ── 交易统计 ──
    lines += ['### 交易统计', '']
    # 平台统计口径：只计普通成交（除权除息行不计入金额/费用/平仓）
    txs = [t for t in rd.transactions if t.get('成交类型') in (None, '', '普通成交')]
    profit_count = loss_count = 0
    total_buy = total_sell = 0.0
    commission = 0.0
    for t in txs:
        pnl = t.get('_realized_pnl')
        if pnl is None:
            txt = t.get('平仓盈亏')
            if isinstance(txt, str) and '/' in txt:
                try:
                    pnl = float(txt.split('/')[0].replace(',', '').strip())
                except (ValueError, TypeError):
                    pnl = None
        if pnl is not None and pnl > 0:
            profit_count += 1
        elif pnl is not None and pnl < 0:
            loss_count += 1
        money = float(t.get('成交金额') or 0)
        if t.get('买/卖') == '买入':
            total_buy += money
        elif t.get('买/卖') == '卖出':
            total_sell += money
        commission += float(t.get('交易费用') or 0)
    lines.append(f'- 总平仓次数: {profit_count + loss_count}, 盈利: {profit_count}, 亏损: {loss_count}')
    lines.append(f'- 总买入金额: {total_buy:,.2f}, 总卖出金额: {total_sell:,.2f}')
    lines.append(f'- 总手续费: {round(commission, 2):,.2f}')
    lines.append(f'- 最终组合价值: {round(float(pv.iloc[-1]), 2):,.2f}')
    lines.append('')
    return '\n'.join(lines)
