"""
模拟盘可视化数据装配 —— 状态库 → 纯 dict（不含任何 HTML）
==========================================================

供 `dashboard.py` 渲染网页用。只读状态库（`paper/store.py` 的 read_* 方法），
不写库、不做格式化，所有数值保持原始类型，格式化交给渲染层。

数据来源与页面对应：
    策略列表  ← strategies + account_performances + account_cash
    净值曲线  ← account_performances（含 benchmark_json）
    回撤      ← account_performances.max_drawdown（平台口径：滚动只增不减）
    当前持仓  ← account_positions（最新已跑交易日）
    历史成交  ← account_trades（全部交易日）
    委托记录  ← account_orders（全部交易日）
    明日计划  ← account_planned_orders（未终结状态 = 待撮合）
    运行日志  ← run_logs
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import DIRECTION_BUY, PLANNED_STATUS_TEXT
from .store import StateStore

# 「操作」列买卖文案：对齐平台详情页用单字（「买」/「卖」），不是「买入」/「卖出」。
# 三张表（计划交易 / 交易详情 / 委托记录）与先卖后买排序共用这两个常量。
SIDE_BUY = '买'
SIDE_SELL = '卖'

# 引擎 OrderStatus 码（engine/smalltrader.py 的 OrderStatus 枚举）→ 中文。
# ⚠ 与 constants.ORDER_STATUS_TEXT（平台 SDK 字段码，ALLTRADED=2）**编号不同**：
# 引擎导出 account_orders.order_status 用的是引擎自己的枚举（ALLTRADED=5），
# 直接用平台码表会把"全部成交"显示成"已拒绝"。此处按引擎码解释。
_ENGINE_ORDER_STATUS_TEXT = {
    0: '待提交', 1: '提交中', 2: '已接受', 3: '未成交', 4: '部分成交',
    5: '全部成交', 6: '已撤单', 7: '部分撤单', 8: '已拒绝',
    9: '待撤单', 10: '部分待撤单', 11: '已过期', 12: '未提交',
}


def _json(raw: Any, default):
    """解析库里存的 JSON 文本列；坏值退回 default。"""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ── 证券简称兜底（状态库 name 列为空时）────────────────────────────
_LOCAL_DS = None            # 本地数据源单例（惰性建，建失败留 None）
_LOCAL_DS_TRIED = False
_NAME_CACHE: Dict[str, str] = {}   # instrument -> 简称；'' 表示查过但确认无


def _local_instrument_names(instruments: List[str]) -> Dict[str, str]:
    """从本地名称表（cn_stock_instruments / cn_fund_instruments）取证券简称。

    状态库的 name 列由引擎导出时写入；早于该回填的批次恒为空串，看板便只能
    显示代码。这里按需查本地名称表兜底，并把结果缓存（含查不到的负结果）。
    惰性导入 + 全程 try：缺 duckdb、缺数据目录、缺表都不应拖垮整个页面 ——
    名称缺失只影响显示，退化为显示代码即可。
    """
    global _LOCAL_DS, _LOCAL_DS_TRIED
    todo = sorted({str(i) for i in instruments if i})
    missing = [i for i in todo if i not in _NAME_CACHE]
    if missing:
        if not _LOCAL_DS_TRIED:
            _LOCAL_DS_TRIED = True
            try:
                from .local_data import LocalParquetDataSource
                _LOCAL_DS = LocalParquetDataSource()
            except Exception:
                _LOCAL_DS = None
        if _LOCAL_DS is not None:
            try:
                found = _LOCAL_DS.get_instrument_names(missing)
            except Exception:
                found = {}
            _NAME_CACHE.update({i: '' for i in missing})   # 负结果也缓存
            _NAME_CACHE.update({str(k): str(v) for k, v in found.items() if v})
    return {i: _NAME_CACHE[i] for i in todo if _NAME_CACHE.get(i)}


def _obj(v: Any) -> Dict[str, Any]:
    """绩效行里的 JSON 列（risk_indicator/benchmark）已被 store._fetch 解析成
    dict 并去掉了 *_json 后缀；这里再兜一层原始字符串，避免两种形态混用出错。"""
    if isinstance(v, dict):
        return v
    return _json(v, {})


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════
# 策略概览（列表页一行）
# ══════════════════════════════════════════════════════════════════
def strategy_summary(store: StateStore, s: Dict[str, Any]) -> Dict[str, Any]:
    """单个策略的概览行（列表页用）。"""
    sid = str(s['strategy_id'])
    perfs = store.read_performances(sid)
    last = perfs[-1] if perfs else None
    day = str(last['trading_day']) if last else None
    risk = _obj(last.get('risk_indicator') if last else None)
    bm = _obj(last.get('benchmark') if last else None)
    positions = store.read_positions(sid, day) if day else []
    pending = store.read_planned_orders(sid, only_pending=True)
    # 近一周收益：沿净值序列回溯 5 个交易日（不足则用现有全部）。
    # 对齐平台列表页总览的「近一周收益」；列表页顶部会把它按组合合并。
    week_return = None
    if len(perfs) >= 2:
        back = min(5, len(perfs) - 1)
        base = _f(perfs[-1 - back]['portfolio_value'])
        if base:
            week_return = _f(perfs[-1]['portfolio_value']) / base - 1.0
    return dict(
        strategy_id=sid,
        strategy_name=str(s['strategy_name']),
        status=str(s.get('status') or 'running'),
        market=str(s.get('market') or ''),
        capital_base=_f(s.get('capital_base')),
        benchmark=str(s.get('benchmark_instrument') or '000300.SH'),
        first_trading_day=s.get('first_trading_day'),
        last_trading_day=s.get('last_trading_day'),
        run_days=len(perfs),
        portfolio_value=_f(last['portfolio_value']) if last else None,
        today_return=_f(last['today_return']) if last else None,
        cumulative_return=_f(last['cumulative_return']) if last else None,
        annualized_return=_f(last['annualized_return']) if last else None,
        max_drawdown=_f(last['max_drawdown']) if last else None,
        week_return=week_return,
        win_percent=_f(last['win_percent']) if last else None,
        pnl_ratio=_f(last['pnl_ratio']) if last else None,
        sharpe=_f(risk.get('sharpe')),
        ir=_f(risk.get('ir')),
        alpha=_f(risk.get('alpha')),
        beta=_f(risk.get('beta')),
        volatility=_f(risk.get('volatility')),
        benchmark_cum_return=_f(bm.get('cum_return')),
        excess_cum_return=_f(bm.get('alpha_cum_return')),
        position_count=len(positions),
        pending_count=len(pending),
        source=str((s.get('config') or {}).get('source') or ''),
    )


def list_strategies(store: StateStore) -> List[Dict[str, Any]]:
    """全部已注册策略的概览（按创建时间）。"""
    return [strategy_summary(store, s) for s in store.list_strategies()]


def portfolio_overview(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把全部账户**当成一个组合**合并，供列表页顶部「模拟交易总览」用。

    口径（页面文案与 `docs/我的交易页面样式对齐说明.md` 都依赖这里，改前先读）：

      总资产     Σ 各账户最新净值
      累计收益   (Σ净值 − Σ初始资金) / Σ初始资金
      今日收益   Σ当日盈亏 / Σ前一日总资产；前一日 = 净值 / (1 + 当日收益率)
      近一周收益 Σ净值 / Σ一周前净值 − 1；一周前 = 净值 / (1 + 近一周收益率)
      最大回撤   max(各账户最大回撤) —— **不做跨账户曲线合成**，
                 取最保守值；它不等于「组合真实回撤」，所以页面必须标清楚
      夏普比率   按初始资金加权的各账户夏普

    缺数据的账户整条跳过（净值缺失），不让 None 参与算术。
    """
    cap = pv = 0.0
    today_pv = today_prev = 0.0
    week_pv = week_ago = 0.0
    sharpe_acc = weight = 0.0
    mdd = None
    day = None
    for r in rows:
        v = r.get('portfolio_value')
        if v is None:
            continue
        v = _f(v)
        cb = _f(r.get('capital_base'))
        pv += v
        cap += cb
        r_today = r.get('today_return')
        if r_today is not None and 1.0 + r_today:
            today_pv += v
            today_prev += v / (1.0 + r_today)
        r_week = r.get('week_return')
        if r_week is not None and 1.0 + r_week:
            week_pv += v
            week_ago += v / (1.0 + r_week)
        sh = r.get('sharpe')
        if sh is not None and cb > 0:
            sharpe_acc += _f(sh) * cb
            weight += cb
        md = r.get('max_drawdown')
        if md is not None:
            mdd = _f(md) if mdd is None else max(mdd, _f(md))
        lt = r.get('last_trading_day')
        if lt and (day is None or str(lt) > day):
            day = str(lt)
    return dict(
        count=len(rows),
        running=sum(1 for r in rows if r.get('status') != 'stopped'),
        day=day,
        capital=cap or None,
        portfolio_value=pv or None,
        # 分子分母必须取自同一批账户：新账户没有「一周前净值」，
        # 若只把它算进分子，合并收益会被凭空放大（曾出现 +96% 的假值）。
        today_return=(today_pv / today_prev - 1.0) if today_prev else None,
        week_return=(week_pv / week_ago - 1.0) if week_ago else None,
        cumulative_return=((pv - cap) / cap) if cap else None,
        max_drawdown=mdd,
        sharpe=(sharpe_acc / weight) if weight else None,
    )


# ══════════════════════════════════════════════════════════════════
# 任务管理器（左栏列表 + 右栏运行历史）
# ══════════════════════════════════════════════════════════════════
# 任务状态 → 中文标签（对齐平台徽标：运行中 / 已完成 / 失败 / 已停止）
TASK_STATUS_TEXT = {'running': '运行中', 'ok': '已完成', 'error': '失败',
                    'stopped': '已停止'}


def strategy_status_text(status: Optional[str]) -> str:
    """strategies.status（部署状态列）的中文标签。

    只有两种：「已停止」= 主动停过；其余（running / ok / error / 空）一律
    「运行中」—— 这是**部署**状态，不是任务状态。跑完一天后 status 会落到
    'ok'，照字面显示「已完成」会让人以为策略不跑了，其实只是今天跑完了，
    明天照常。运行结果的成败看「最近运行时间」列那枚徽标。
    """
    return '已停止' if str(status or '') == 'stopped' else '运行中'


def task_status_text(status: Optional[str]) -> str:
    """任务状态中文标签；空状态给 '—'。

    以前默认值是「待运行」，但它会被当成 `strategies.status` 的兜底值 ——
    那是个状态列、不是任务状态列，「待运行」对从未跑过的账户是误导，故改 '—'。
    """
    return TASK_STATUS_TEXT.get(str(status or ''), '—')


def _task_run_view(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not run:
        return None
    return dict(
        run_id=str(run.get('run_id') or ''),
        kind=str(run.get('kind') or 'run'),
        status=str(run.get('status') or ''),
        status_text=task_status_text(run.get('status')),
        mode=run.get('mode') or '',
        trading_day=run.get('trading_day') or '',
        summary=str(run.get('summary') or ''),
        error=str(run.get('error') or ''),
        started_at=run.get('started_at') or '',
        finished_at=run.get('finished_at') or '',
        log=list(run.get('log') or []),
        params=dict(run.get('params') or {}),
    )


def task_list(store: StateStore) -> List[Dict[str, Any]]:
    """任务管理器左栏数据：每个已注册策略一行，带最近一次运行结果。"""
    out = []
    for s in store.list_strategies():
        row = strategy_summary(store, s)
        row['last_run'] = _task_run_view(store.latest_task_run(row['strategy_id']))
        out.append(row)
    return out


def task_runs(store: StateStore, strategy_id: str,
              limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """某策略的运行历史（最新在前，含逐行日志）。"""
    return [_task_run_view(r) for r in store.read_task_runs(strategy_id, limit=limit)]


def latest_run_times(store: StateStore) -> Dict[str, Dict[str, Any]]:
    """{strategy_id: {started_at, status}} —— 列表页「最近运行时间」列。

    开始运行 + 状态合并成一次查询，避免列表页每行各查一遍 task_runs。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for sid, r in store.latest_run_times().items():
        out[sid] = dict(started_at=r.get('started_at') or '',
                        status=str(r.get('status') or ''),
                        trading_day=str(r.get('trading_day') or ''))
    return out


# ══════════════════════════════════════════════════════════════════
# 净值序列（图表 + 表格视图）
# ══════════════════════════════════════════════════════════════════
_RANGE_DAYS = {'1m': 30, '3m': 90, '6m': 180, '1y': 365}


def equity_series(store: StateStore, strategy_id: str,
                  rng: str = 'all') -> Dict[str, Any]:
    """收益率走势序列（对齐 BigQuant「收益率走势」区块的口径与配色）。

    三条曲线同为累计收益率（区间首日 = 0，同一单位、单轴可比）：
        策略收益率   cumulative_return       （红）
        相对收益率   benchmark.alpha_cum_return（橙）
        基准指数     benchmark.cum_return    （蓝）
    另返回逐日**瞬时回撤**（负数，画在 0 轴下方）与逐日表格行。

    回撤口径说明：库里存的 `max_drawdown` 是平台口径的**滚动最大回撤**（只增不减，
    创新高也不回落）——直接画出来是一条阶梯线，不是回撤曲线。这里按标准做法
    由总资产序列重算**当日回撤** = (区间内历史峰值 − 当日总资产) / 历史峰值，
    创新高回到 0、下跌时加深；「最大回撤」指标卡仍用平台的滚动口径值。

    rng: 'all' | '1m' | '3m' | '6m' | '1y' —— 以**最新已跑交易日**为锚点按自然日
    回推（不用系统当天做锚，否则数据滞后时"近一月"会查成空区间）。
    """
    perfs = store.read_performances(strategy_id)
    if rng in _RANGE_DAYS and perfs:
        anchor = datetime.strptime(str(perfs[-1]['trading_day']), '%Y-%m-%d')
        start = (anchor - timedelta(days=_RANGE_DAYS[rng])).strftime('%Y-%m-%d')
        perfs = [p for p in perfs if str(p['trading_day']) >= start]
    if not perfs:
        return dict(dates=[], strat=[], bench=[], excess=[], drawdown=[],
                    rows=[], range_return=None, range_max_dd=None,
                    max_dd=0.0)   # 详情页渲染以 max_dd/range_max_dd 为键，空态也要给齐

    dates, strat, bench, excess, dd, ret, pvs = [], [], [], [], [], [], []
    peak = 0.0
    # 重定基到区间首日 = 0：库里存的 cumulative_return / cum_return 是**成立以来**
    # 的累计值，直接画会让"近一月"曲线起点不为零、区间收益也变成全区间值。
    # 相对收益率 = 重定基后的策略收益 − 重定基后的基准收益（与库内 alpha_cum_return
    # 的口径一致）；回撤本来就是区间内重算的，无需再处理。
    base_s = _f(perfs[0]['cumulative_return'])
    base_b = _f(_obj(perfs[0].get('benchmark')).get('cum_return'))
    for p in perfs:
        bm = _obj(p.get('benchmark'))
        pv = _f(p['portfolio_value'])
        peak = max(peak, pv)
        s_abs = _f(p['cumulative_return'])
        b_abs = _f(bm.get('cum_return'))
        s_rel = (1.0 + s_abs) / (1.0 + base_s) - 1.0 if base_s > -1 else s_abs - base_s
        b_rel = (1.0 + b_abs) / (1.0 + base_b) - 1.0 if base_b > -1 else b_abs - base_b
        dates.append(str(p['trading_day']))
        strat.append(s_rel)
        bench.append(b_rel)
        excess.append(s_rel - b_rel)
        dd.append(-((peak - pv) / peak) if peak > 0 else 0.0)
        ret.append(_f(p['today_return']))
        pvs.append(pv)

    rows = []
    for i, p in enumerate(perfs):
        rows.append(dict(
            trading_day=dates[i],
            portfolio_value=pvs[i],
            today_return=ret[i],
            cumulative_return=strat[i],
            benchmark_cum_return=bench[i],
            excess_cum_return=excess[i],
            drawdown=dd[i],
            max_drawdown=_f(p['max_drawdown']),      # 平台滚动口径（只增不减）
        ))
    return dict(dates=dates, strat=strat, bench=bench, excess=excess,
                drawdown=dd, rows=rows,
                range_return=strat[-1],
                range_max_dd=max((abs(v) for v in dd), default=0.0),
                max_dd=_f(perfs[-1]['max_drawdown']))    # 平台滚动口径，画参考线


# ══════════════════════════════════════════════════════════════════
# 详情页数据
# ══════════════════════════════════════════════════════════════════
def _position_row(p: Dict[str, Any], pv_by_day: Dict[str, float],
                  names: Dict[str, str],
                  hold_seq: Optional[Dict[str, Dict[str, int]]] = None
                  ) -> Dict[str, Any]:
    day = str(p['trading_day'])
    inst = str(p['instrument'])
    mv = _f(p.get('market_value'))
    pv = pv_by_day.get(day, 0.0)
    qty = int(p.get('current_qty') or 0)
    cost = _f(p.get('cost_price'))
    last = _f(p.get('last_price'))
    # 引擎导出的 position_pnl / profit_ratio / hold_days 恒为 0（未落值），
    # 展示层按「成本价 × 数量 × 最新价」与持仓历史补齐，与平台列口径一致。
    pnl = _f(p.get('position_pnl')) or ((last - cost) * qty if cost and last else 0.0)
    ratio = _f(p.get('profit_ratio')) or ((last / cost - 1.0) if cost else 0.0)
    hold_days = int(p.get('hold_days') or 0) or \
        ((hold_seq or {}).get(inst, {}).get(day, 0))
    return dict(
        trading_day=day,
        instrument=inst,
        name=str(p.get('name') or names.get(inst, '')),
        cost_price=cost,
        current_qty=qty,
        hold_days=hold_days,
        position_ratio=(mv / pv if pv else 0.0),      # 仓位占比 = 市值 / 当日总资产
        last_price=last,
        market_value=mv,
        position_pnl=pnl,
        profit_ratio=ratio,
    )


def _trade_row(t: Dict[str, Any], order_by_key: Dict[str, Dict[str, Any]],
               names: Dict[str, str]) -> Dict[str, Any]:
    o = order_by_key.get(str(t.get('order_key') or ''), {})
    return dict(
        trading_day=str(t['trading_day']),
        filled_dt=str(t.get('filled_dt') or ''),
        instrument=str(t['instrument']),
        name=str(t.get('name') or names.get(str(t['instrument']), '')),
        direction=int(t.get('direction') or 0),
        side=SIDE_BUY if int(t.get('direction') or 0) == DIRECTION_BUY else SIDE_SELL,
        filled_qty=int(t.get('filled_qty') or 0),
        filled_price=_f(t.get('filled_price')),
        filled_money=_f(t.get('filled_money')),
        commission=_f(t.get('commission')),
        realized_pnl=_f(t.get('realized_pnl')),
        # 委托数量/委托状态：交易详情里与委托记录同源（按 order_key 关联）
        order_qty=int(o.get('order_qty') or t.get('filled_qty') or 0),
        status_text=_ENGINE_ORDER_STATUS_TEXT.get(
            int(o.get('order_status') or 0), '—') if o else '—',
    )


def _order_row(o: Dict[str, Any],
               day_by_key: Optional[Dict[str, str]] = None,
               names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    st = int(o.get('order_status') or 0)
    # 引擎导出的 orders.trading_day 为空（计划单注入时 current_dt 尚未设置），
    # 用 order_key 从成交/计划单回填，否则「委托记录」日期列整列为空。
    day = str(o.get('trading_day') or '')
    if not day and day_by_key:
        day = day_by_key.get(str(o.get('order_key') or ''), '')
    return dict(
        trading_day=day,
        instrument=str(o['instrument']),
        name=str(o.get('name') or (names or {}).get(str(o['instrument']), '')),
        side=SIDE_BUY if int(o.get('direction') or 0) == DIRECTION_BUY else SIDE_SELL,
        order_qty=int(o.get('order_qty') or 0),
        filled_qty=int(o.get('filled_qty') or 0),
        order_price=_f(o.get('order_price')),
        average_price=_f(o.get('average_price')),
        order_status=st,
        status_text=_ENGINE_ORDER_STATUS_TEXT.get(st, str(st)),
        status_msg=str(o.get('status_msg') or ''),
        commission=_f(o.get('commission')),
    )


def _planned_row(p: Dict[str, Any],
                 names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    st = int(p.get('order_status') or 0)
    return dict(
        execute_day=str(p['trading_day']),
        signal_day=str(p.get('signal_day') or ''),
        planned_order_id=str(p.get('planned_order_id') or ''),
        instrument=str(p['instrument']),
        name=str(p.get('name') or (names or {}).get(str(p['instrument']), '')),
        side=SIDE_BUY if int(p.get('direction') or 0) == DIRECTION_BUY else SIDE_SELL,
        order_qty=int(p.get('order_qty') or 0),
        order_price=_f(p.get('order_price')),
        order_status=st,
        status_text=PLANNED_STATUS_TEXT.get(st, str(st)),
        order_dt=str(p.get('order_dt') or ''),
    )


def strategy_detail(store: StateStore, strategy_id: str) -> Optional[Dict[str, Any]]:
    """详情页全部数据；策略不存在返回 None。"""
    s = store.get_strategy(strategy_id)
    if s is None:
        return None
    summary = strategy_summary(store, s)
    raw_orders = store.read_orders(strategy_id)
    raw_trades = store.read_trades(strategy_id)

    raw_planned = store.read_planned_orders(strategy_id)

    # 名称映射：持仓行的 name 列为空，用成交/委托/计划单里的名称补
    names: Dict[str, str] = {}
    for src in (raw_trades, raw_orders, raw_planned):
        for r in src:
            nm = str(r.get('name') or '')
            if nm:
                names.setdefault(str(r['instrument']), nm)

    # 当日总资产（持仓详情算仓位占比用）
    pv_by_day = {str(p['trading_day']): _f(p['portfolio_value'])
                 for p in store.read_performances(strategy_id)}

    # 持仓详情：历史每日持仓（对齐平台：一行 = 某交易日某标的的持仓），新→旧
    raw_positions = store.read_positions(strategy_id, None)

    # 库里仍缺的名称（老批次写入时引擎未回填 name）→ 查本地名称表补齐，
    # 保证持仓/成交/委托/计划单四处显示口径一致
    names.update({k: v for k, v in _local_instrument_names(
        [str(r['instrument']) for r in
         list(raw_positions) + list(raw_trades) + list(raw_orders)
         + list(raw_planned)]).items() if k not in names})

    # 持仓天数：该标的在持仓历史中出现的第几天（引擎 hold_days 恒为 0）
    days_by_inst: Dict[str, set] = {}
    for p in raw_positions:
        days_by_inst.setdefault(str(p['instrument']), set()).add(
            str(p['trading_day']))
    hold_seq = {inst: {d: i + 1 for i, d in enumerate(sorted(ds))}
                for inst, ds in days_by_inst.items()}
    positions = [_position_row(p, pv_by_day, names, hold_seq)
                 for p in raw_positions]
    positions.sort(key=lambda r: (r['trading_day'], -r['market_value']),
                   reverse=True)

    # order_key → 委托行（交易详情展示委托数量/状态）
    order_by_key = {str(o['order_key']): o for o in raw_orders if o.get('order_key')}
    trades = [_trade_row(t, order_by_key, names) for t in raw_trades]
    trades.reverse()                                   # 最新在前

    # order_key → 交易日（先成交、再计划单），用于回填 orders 的空日期
    day_by_key: Dict[str, str] = {
        str(t['order_key']): str(t['trading_day'])
        for t in raw_trades if t.get('order_key')}
    for p in raw_planned:
        if p.get('order_key'):
            day_by_key.setdefault(str(p['order_key']), str(p['trading_day']))
    orders = [_order_row(o, day_by_key, names) for o in raw_orders]
    orders.reverse()

    pending = [_planned_row(p, names)
               for p in store.read_planned_orders(strategy_id, only_pending=True)]
    # 先卖后买（先腾现金），与实盘下单习惯一致；再按执行日、代码排
    pending.sort(key=lambda r: (r['execute_day'],
                                r['side'] != SIDE_SELL, r['instrument']))

    logs = []
    for lg in store.read_run_logs(strategy_id):
        logs.append(dict(trading_day=str(lg['trading_day']),
                         level=str(lg.get('level') or 'INFO'),
                         msg=str(lg.get('msg') or '')))
    logs.reverse()

    return dict(meta=summary, positions=positions, trades=trades, orders=orders,
                planned=pending,
                planned_days=sorted({r['execute_day'] for r in pending}),
                logs=logs)


# ══════════════════════════════════════════════════════════════════
# 策略文件发现（未注册的策略也能一键首跑）
# ══════════════════════════════════════════════════════════════════
def discover_strategy_files(project_root: Path,
                            registered_ids: List[str]) -> List[Dict[str, Any]]:
    """扫描 strategies/*.py 与 *.ipynb，读出策略元信息并标注是否已注册。

    加载策略文件会执行其顶层代码（与 run_daily.py 同款做法），因此结果按
    「目录 mtime + 30 秒」缓存，避免每次刷新列表页都重跑一遍策略模块。
    坏文件不抛异常，而是把错误信息带出来在页面上显示。
    """
    sdir = Path(project_root) / 'strategies'
    if not sdir.is_dir():
        return []
    key = str(sdir)
    try:
        mtime = max((f.stat().st_mtime for f in _iter_strategy_files(sdir)),
                    default=0.0)
    except OSError:
        mtime = 0.0
    cached = _FILE_CACHE.get(key)
    now = time.time()
    if cached and cached[0] == mtime and now - cached[1] < 30:
        items = cached[2]
    else:
        items = _scan_strategy_files(sdir)
        _FILE_CACHE[key] = (mtime, now, items)
    # registered 每次重算：状态库随时可能新增账户
    return [dict(it, registered=it['strategy_id'] in registered_ids) for it in items]


_FILE_CACHE: Dict[str, tuple] = {}

# 策略目录里可运行的文件（.py 与 .ipynb 同权）
STRATEGY_GLOBS = ('*.py', '*.ipynb')


def _iter_strategy_files(sdir: Path) -> List[Path]:
    files = []
    for pat in STRATEGY_GLOBS:
        files.extend(sdir.glob(pat))
    return [f for f in files if not f.name.startswith('_')]


def _scan_strategy_files(sdir: Path) -> List[Dict[str, Any]]:
    from .runner import StrategySpec
    out: List[Dict[str, Any]] = []
    for f in sorted(_iter_strategy_files(sdir)):
        item: Dict[str, Any] = dict(path=str(f), file=f.name,
                                    strategy_id=None, strategy_name=None,
                                    instruments=0, capital_base=None,
                                    error=None)
        try:
            spec = StrategySpec.load(str(f))
            item.update(strategy_id=spec.strategy_id,
                        strategy_name=spec.strategy_name,
                        instruments=len(spec.instruments),
                        capital_base=spec.capital_base)
        except Exception as e:                      # 坏文件不阻断页面
            item['error'] = f'{type(e).__name__}: {e}'
        out.append(item)
    return out
