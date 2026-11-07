"""`m_*` 时序函数的本地映射（沿每只标的按日期滚动）。

对应平台语义：在 `PARTITION BY instrument ORDER BY date` 上做窗口计算。
纯窗口能表达的走 SQL；需要逐行状态的（加权、相关、滚动排名）交给 ta.py 的 UDF。
"""
from __future__ import annotations

from .registry import FuncSpec, register

SEQ_OVER = 'PARTITION BY instrument ORDER BY date'

# 滚动聚合：n = 窗口长度（含当日）
_AGG = {'avg': 'avg', 'mean': 'avg', 'sum': 'sum', 'stddev': 'stddev_samp',
        'std': 'stddev_samp', 'var': 'var_samp', 'max': 'max', 'min': 'min',
        'median': 'median', 'count': 'count'}


def _int_arg(value: str, func: str) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        from .errors import DialectError
        raise DialectError(
            f'{func} 的窗口长度必须是整数字面量，实际是 {value!r}；'
            '本地按字面量展开窗口边界，无法接受列名或表达式') from None
    if n <= 0:
        from .errors import DialectError
        raise DialectError(f'{func} 的窗口长度必须为正，实际 {n}')
    return n


def _frame(n: int) -> str:
    return f'ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW'


# ── 移位类 ────────────────────────────────────────────────────────────────
def _shift(name: str, sql_fn: str):
    def build(args, named):
        col = args[0]
        n = _int_arg(args[1] if len(args) > 1 else named.get('n', '1'), name)
        return f'{sql_fn}({col}, {n}) OVER ({SEQ_OVER})'
    return build


register(FuncSpec('m_lag', 'seq', _shift('m_lag', 'lag'),
                  doc='沿标的取前 n 行（默认 1）', min_args=1, max_args=2))
register(FuncSpec('m_lead', 'seq', _shift('m_lead', 'lead'),
                  doc='沿标的取后 n 行（默认 1）', min_args=1, max_args=2))


def _delta(args, named):
    col = args[0]
    n = _int_arg(args[1] if len(args) > 1 else named.get('n', '1'), 'm_delta')
    return (f'({col} - lag({col}, {n}) OVER ({SEQ_OVER}))')


def _returns(args, named):
    col = args[0]
    n = _int_arg(args[1] if len(args) > 1 else named.get('n', '1'), 'm_returns')
    return (f'({col} / nullif(lag({col}, {n}) OVER ({SEQ_OVER}), 0) - 1)')


register(FuncSpec('m_delta', 'seq', _delta,
                  doc='相邻 n 行差值', min_args=1, max_args=2))
register(FuncSpec('m_returns', 'seq', _returns,
                  doc='相邻 n 行收益率 x/lag(x,n)-1', min_args=1, max_args=2))


# ── 滚动聚合 ──────────────────────────────────────────────────────────────
def _aggregate(name: str, sql_agg: str):
    def build(args, named):
        col = args[0]
        raw = args[1] if len(args) > 1 else named.get('n') or named.get('window')
        n = _int_arg(raw, name)
        over = f'{SEQ_OVER} {_frame(n)}'
        # 窗口不足不计算：平台/pandas 口径都是"前 n-1 行为 NULL"，
        # 若按 ROWS n-1 PRECEDING 直接算会得到部分窗口值（前几行有数），
        # 策略里 `len(series) >= n` 这类守卫就对不上了。
        return (f'CASE WHEN count({col}) OVER ({over}) = {n} '
                f'THEN {sql_agg}({col}) OVER ({over}) END')
    return build


for _alias, _sql in _AGG.items():
    register(FuncSpec(f'm_{_alias}', 'seq', _aggregate(f'm_{_alias}', _sql),
                      doc=f'滚动 {_sql}（窗口含当日）', min_args=2, max_args=2))


# ── 需要 UDF 的时序函数 ────────────────────────────────────────────────────
def _guard(expr: str, col: str, n: int) -> str:
    """窗口不足 n 行时返回 NULL（与平台/pandas 口径一致）。"""
    if n <= 0:
        return expr
    over = f'{SEQ_OVER} {_frame(n)}'
    return (f'CASE WHEN count({col}) OVER ({over}) >= {n} '
            f'THEN {expr} END')


def _rolling_rank_pct(args, named):
    col = args[0]
    n = _int_arg(args[1] if len(args) > 1 else named.get('n', '0'), 'm_rolling_rank')
    expr = f'bq_roll_rank_pct(list({col}) OVER ({SEQ_OVER}), {n})'
    return _guard(expr, col, n)


def _pct_rank_seq(args, named):
    col = args[0]
    n = _int_arg(args[1] if len(args) > 1 else named.get('n', '0'), 'm_pct_rank')
    return _rolling_rank_pct([col, str(n)], {})


def _corr(args, named):
    y, x = args[0], args[1]
    n = _int_arg(args[2] if len(args) > 2 else named.get('n', '0'), 'm_corr')
    expr = (f'bq_roll_corr(list({y}) OVER ({SEQ_OVER}), '
            f'list({x}) OVER ({SEQ_OVER}), {n})')
    return _guard(expr, y, n)


def _decay_linear(args, named):
    col = args[0]
    n = _int_arg(args[1] if len(args) > 1 else named.get('n', '0'), 'm_decay_linear')
    expr = f'bq_decay_linear(list({col}) OVER ({SEQ_OVER}), {n})'
    return _guard(expr, col, n)


register(FuncSpec('m_rolling_rank', 'seq_udf', _rolling_rank_pct,
                  doc='当前值在过去 n 行中的百分位（0~1）', min_args=1, max_args=2))
register(FuncSpec('m_pct_rank', 'seq_udf', _pct_rank_seq,
                  doc='同 m_rolling_rank（平台两种写法都出现）', min_args=1, max_args=2))
register(FuncSpec('m_corr', 'seq_udf', _corr,
                  doc='过去 n 行两列相关系数', min_args=2, max_args=3))
register(FuncSpec('m_decay_linear', 'seq_udf', _decay_linear,
                  doc='过去 n 行线性加权均值（近端权重大）', min_args=1, max_args=2))
