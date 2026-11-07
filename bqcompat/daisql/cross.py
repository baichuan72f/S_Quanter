"""`c_*` 截面函数的本地映射（同一天横切）。

对应平台语义：在 `PARTITION BY date` 上做窗口计算。
"""
from __future__ import annotations

from .errors import DialectError
from .registry import FuncSpec, register

CROSS_OVER = 'PARTITION BY date'

_AGG = {'avg': 'avg', 'mean': 'avg', 'sum': 'sum', 'std': 'stddev_samp',
        'stddev': 'stddev_samp', 'var': 'var_samp', 'max': 'max', 'min': 'min',
        'median': 'median', 'count': 'count'}
_RANK = {'rank': 'rank', 'dense_rank': 'dense_rank', 'pct_rank': 'percent_rank'}


def _agg(name: str, sql_agg: str):
    def build(args, named):
        col = args[0]
        return f'{sql_agg}({col}) OVER ({CROSS_OVER})'
    return build


def _rank(name: str, sql_rank: str):
    def build(args, named):
        col = args[0]
        # 空值必须保持空值：DuckDB 的 ORDER BY 默认把 NULL 排在末尾，
        # 直接算排名会给"当天没行情"的标的排到最高/最低位，与截面语义不符。
        return (f'CASE WHEN {col} IS NULL THEN NULL ELSE '
                f'{sql_rank}() OVER ({CROSS_OVER} ORDER BY {col}) END')
    return build


def _zscore(args, named):
    col = args[0]
    return (f'({col} - avg({col}) OVER ({CROSS_OVER})) '
            f'/ nullif(stddev_samp({col}) OVER ({CROSS_OVER}), 0)')


def _normalize(args, named):
    col = args[0]
    return (f'({col} - min({col}) OVER ({CROSS_OVER})) '
            f'/ nullif(max({col}) OVER ({CROSS_OVER}) '
            f'- min({col}) OVER ({CROSS_OVER}), 0)')


def _quantile(args, named):
    col = args[0]
    q = args[1] if len(args) > 1 else named.get('q', '0.5')
    return f'quantile_cont({col}, {q}) OVER ({CROSS_OVER})'


def _neutralize(args, named):
    """截面线性回归取残差：resid = y - (a + b*x)。

    b = corr(y,x) * std(y) / std(x)（截面内），a = avg(y) - b*avg(x)。
    corr 用 UDF（DuckDB 没有窗口相关函数），其余走窗口聚合。
    注意：平台若做的是"行业/市值中性化"，口径会更复杂，这里只给线性回归残差。
    """
    y = args[0]
    if len(args) < 2:
        raise DialectError(
            'c_neutralize 需要两个参数：c_neutralize(值列, 中性化基准列)；'
            f'实际只有 {len(args)} 个')
    x = args[1]
    corr = f'bq_cs_corr(list({y}) OVER ({CROSS_OVER}), list({x}) OVER ({CROSS_OVER}))'
    std_y = f'stddev_samp({y}) OVER ({CROSS_OVER})'
    std_x = f'stddev_samp({x}) OVER ({CROSS_OVER})'
    b = f'({corr} * {std_y} / nullif({std_x}, 0))'
    a = f'(avg({y}) OVER ({CROSS_OVER}) - {b} * avg({x}) OVER ({CROSS_OVER}))'
    return f'({y} - {a} - {b} * {x})'


for _alias, _sql in _AGG.items():
    register(FuncSpec(f'c_{_alias}', 'cross', _agg(f'c_{_alias}', _sql),
                      doc=f'当日截面 {_sql}', min_args=1, max_args=1))

for _alias, _sql in _RANK.items():
    register(FuncSpec(f'c_{_alias}', 'cross', _rank(f'c_{_alias}', _sql),
                      doc=f'当日截面 {_sql}', min_args=1, max_args=1))

register(FuncSpec('c_zscore', 'cross', _zscore,
                  doc='当日截面 z-score：(x-均值)/标准差', min_args=1, max_args=1))
register(FuncSpec('c_z_normalize', 'cross', _zscore,
                  doc='同 c_zscore（平台两种写法都有）', min_args=1, max_args=1))
register(FuncSpec('c_normalize', 'cross', _normalize,
                  doc='当日截面 min-max 归一', min_args=1, max_args=1))
register(FuncSpec('c_quantile_cont', 'cross', _quantile,
                  doc='当日截面分位数，默认 0.5', min_args=1, max_args=2))
register(FuncSpec('c_neutralize', 'cross', _neutralize,
                  doc='当日截面线性回归残差（近似中性化）', min_args=2, max_args=2))
register(FuncSpec('c_neutralize_resid', 'cross', _neutralize,
                  doc='同 c_neutralize', min_args=2, max_args=2))
