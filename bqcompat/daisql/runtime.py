"""把 daisql 需要的 UDF 注册进 DuckDB 连接（幂等）。

`dai_compat._conn()` 建好连接后调用 `ensure_functions(con)`；同一连接只注册一次。
"""
from __future__ import annotations

from typing import Set

_REGISTERED: Set[int] = set()


def ensure_functions(con) -> None:
    """注册全部 UDF；重复调用无副作用。"""
    key = id(con)
    if key in _REGISTERED:
        return

    from . import ta, udfs  # noqa: PLC0415 - 延迟导入，避免循环

    specs = list(udfs.UDFS) + [
        ('bq_ta_ema', ta.udf_ema, ['DOUBLE[]', 'INTEGER'], 'DOUBLE'),
        ('bq_ta_rsi', ta.udf_rsi, ['DOUBLE[]', 'INTEGER'], 'DOUBLE'),
        ('bq_ta_macd_dif', ta.udf_macd_dif,
         ['DOUBLE[]', 'INTEGER', 'INTEGER', 'INTEGER'], 'DOUBLE'),
        ('bq_ta_macd_dea', ta.udf_macd_dea,
         ['DOUBLE[]', 'INTEGER', 'INTEGER', 'INTEGER'], 'DOUBLE'),
        ('bq_ta_kdj_k', ta.udf_kdj_k,
         ['DOUBLE[]', 'DOUBLE[]', 'DOUBLE[]', 'INTEGER', 'INTEGER', 'INTEGER'],
         'DOUBLE'),
        ('bq_ta_kdj_d', ta.udf_kdj_d,
         ['DOUBLE[]', 'DOUBLE[]', 'DOUBLE[]', 'INTEGER', 'INTEGER', 'INTEGER'],
         'DOUBLE'),
        ('bq_ta_kdj_j', ta.udf_kdj_j,
         ['DOUBLE[]', 'DOUBLE[]', 'DOUBLE[]', 'INTEGER', 'INTEGER', 'INTEGER'],
         'DOUBLE'),
    ]
    for name, fn, arg_types, ret in specs:
        try:
            # null_handling='special'：UDF 允许返回 None（数据不足/无法计算），
            # 默认的 'default' 会把返回 None 当成错误直接抛 InvalidInputException。
            con.create_function(name, fn, arg_types, ret,
                                null_handling='special')
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f'注册 UDF {name} 失败: {exc}') from exc

    _REGISTERED.add(key)


def reset() -> None:
    """测试用：清掉注册记录（切换连接时）。"""
    _REGISTERED.clear()
