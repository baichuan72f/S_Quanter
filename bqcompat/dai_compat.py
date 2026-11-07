"""dai 兼容层：平台数据仓库查询 → 本地 DuckDB
================================================

平台上 `dai.query(sql)` 打的是 BigQuant 数据仓库（`cn_stock_prefactors`、
`cn_fund_bar1d` 等表）；本地 `data/` 下是 DAU 日更导出的 parquet 快照，由
`queries.connect()` 建成 DuckDB 视图，**且表名与平台一致**（每张表同时提供
`v_<name>` 与裸名两种视图）。因此这里不做 SQL 改写，直接把用户的 SQL 丢给
DuckDB 就能跑。

平台写法与本地实现的对应：

    dai.query(sql).df()                      → DuckDB 执行后返回 DataFrame
    dai.query(sql, filters={...}).df()       → 追加 WHERE 条件
    dai.DataSource().read_df(table, ...)     → 按表名 + 日期/标的条件读取
    dai.DataSource().write_bdb(...)          → 本地空实现（无平台存储可写）

业主要点
--------
* `filters` 的值有两种形态，要分辨：
  - 日期区间：`{"date": [start, end]}`（长度 2 且两端是日期）→ `BETWEEN`
  - 取值集合：`{"instrument": ["510300.SH", "159915.SZ"]}` → `IN (...)`
* 本地没有的表（成分股、估值、分钟、期货、可转债等）不静默返回空表，
  而是抛 `DaiTableError` 并列出本地可用表名——空表会让策略"静默不下单"，
  这种失败最难查。
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
import types
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

__all__ = ['query', 'DataSource', 'DaiTableError', 'DEFAULT_PARTITION_FIELD',
           'install', 'available_tables']

DEFAULT_PARTITION_FIELD = 'date'

_SUPPORTED = {'cn_stock_real_bar1d', 'cn_stock_bar1d', 'cn_fund_bar1d',
              'cn_fund_real_bar1d', 'cn_stock_index_bar1d', 'cn_stock_prefactors',
              'cn_stock_dividend', 'cn_stock_instruments', 'cn_fund_instruments',
              'trading_days'}

_CONN = None


class DaiTableError(RuntimeError):
    """本地没有这张平台表（或表里没数据）。"""


def _conn():
    """复用 queries.connect() 的 DuckDB 连接（惰性读 parquet，一次建好视图）。

    建好后注册 daisql 需要的 UDF（技术指标/滚动相关等），只做一次。
    """
    global _CONN
    if _CONN is None:
        import queries  # noqa: PLC0415 - 项目根；调用方需保证 sys.path
        from .daisql import runtime  # noqa: PLC0415
        _CONN = queries.connect()
        runtime.ensure_functions(_CONN)
    return _CONN


def available_tables() -> List[str]:
    """本地实际可查的表名（有数据才列）。"""
    import queries  # noqa: PLC0415
    return [t['name'] for t in queries.TABLES if queries.table_exists(t)]


def _is_date_like(v) -> bool:
    if isinstance(v, (_dt.date, _dt.datetime, pd.Timestamp)):
        return True
    return isinstance(v, str) and bool(
        re.fullmatch(r'\d{4}-?\d{2}-?\d{2}(?:[ T].*)?', v.strip()))


def _lit(v) -> str:
    if isinstance(v, (_dt.date, _dt.datetime, pd.Timestamp)):
        return "'" + pd.Timestamp(v).strftime('%Y-%m-%d %H:%M:%S') + "'"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


def _filter_sql(filters: Optional[Dict[str, Any]]) -> str:
    """把平台的 filters 字典翻成 WHERE 片段。"""
    if not filters:
        return ''
    parts: List[str] = []
    for col, val in filters.items():
        if val is None:
            continue
        if isinstance(val, (list, tuple)) and len(val) == 2 \
                and _is_date_like(val[0]) and _is_date_like(val[1]):
            parts.append(f'{col} BETWEEN {_lit(val[0])} AND {_lit(val[1])}')
        elif isinstance(val, (list, tuple, set)):
            items = ', '.join(_lit(x) for x in val)
            parts.append(f'{col} IN ({items})')
        elif isinstance(val, (list, tuple)) and len(val) == 1:
            parts.append(f'{col} = {_lit(val[0])}')
        else:
            parts.append(f'{col} = {_lit(val)}')
    return ' AND '.join(parts)


def _exec(sql: str, filters: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    # 平台 SQL 方言（m_* 时序 / c_* 截面）先翻成 DuckDB 窗口函数
    from . import sql_dialect  # noqa: PLC0415 - 避免循环导入

    sql, _used = sql_dialect.translate(sql)
    where = _filter_sql(filters)
    full = sql
    if where:
        full = f'SELECT * FROM (\n{sql}\n) AS _bq_filtered WHERE {where}'
    try:
        return _conn().execute(full).df()
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        # 非股票表（指数/日历等）没有 instrument 列：把时序窗口退回全局按日期排序
        if 'instrument' in msg and 'PARTITION BY instrument' in full:
            try:
                return _conn().execute(
                    full.replace('PARTITION BY instrument ORDER BY date',
                                 'ORDER BY date')).df()
            except Exception:  # noqa: BLE001 - 落到下面的统一报错
                pass
        # 缺列：本地因子表字段集与云端不同（平台会随版本更新字段），
        # 这类报错最容易让人以为是兼容层坏了，这里把可用列摆出来。
        if 'Referenced column' in msg and 'not found' in msg:
            raise DaiTableError(
                '本地有这张表，但没有你要的字段——本地因子表的字段集与云端不一致'
                '（云端会随版本更新字段）。\n'
                f'原始错误: {msg.splitlines()[0]}\n'
                '处置：①换用本地已有的同义字段；②把该字段导出成新的 parquet '
                '补进 data/<表名>/；③在策略里用现有列自己算出来。\n'
                '逐表字段清单：.venv\\Scripts\\python.exe ..\\reports\\tools\\'
                'list_table_columns.py <表名>') from exc
        missing = [t for t in _SUPPORTED if t not in available_tables()]
        if 'does not exist' in msg or 'Catalog Error' in msg:
            raise DaiTableError(
                '本地没有这张表或表里没数据。\n'
                f'原始错误: {msg}\n'
                f'本地可用表: {", ".join(available_tables())}\n'
                f'（平台有但本地缺: {", ".join(missing) or "无"}）') from exc
        raise


class QueryResult:
    """平台 `dai.query(...)` 的返回值：`.df()` 取 DataFrame。

    其余属性透传给底层 DataFrame（`head()`、`shape`、`columns` 等），
    让「不调 .df() 直接用」的写法也能苟活。
    """

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def df(self) -> pd.DataFrame:
        return self._df

    def to_df(self) -> pd.DataFrame:
        return self._df

    def __getattr__(self, item: str):
        return getattr(self._df, item)

    def __len__(self) -> int:
        return len(self._df)

    def __repr__(self) -> str:
        return f'<dai.QueryResult rows={len(self._df)} cols={list(self._df.columns)[:8]}>'


def query(sql: str, filters: Optional[Dict[str, Any]] = None, **kwargs) -> QueryResult:
    """平台 `dai.query(sql, filters=...)`。

    其余平台参数（`extra_filters` / `partition_field` 等）忽略，避免报错打断策略。
    """
    filters = filters or kwargs.get('extra_filters')
    return QueryResult(_exec(sql, filters))


class DataSource:
    """平台 `dai.DataSource()`：按表名读取，本地落 parquet 视图。"""

    def __init__(self, table: str = '', *args, **kwargs):
        self.table = table or kwargs.get('table', '')

    # -- 读 ------------------------------------------------------------
    def read_df(self, table: str = '', columns: Optional[Iterable[str]] = None,
                start_date: Optional[str] = None, end_date: Optional[str] = None,
                filters: Optional[Dict[str, Any]] = None,
                instruments: Optional[Iterable[str]] = None,
                sql: Optional[str] = None, **kwargs) -> pd.DataFrame:
        table = table or self.table
        if sql:
            return _exec(sql, filters)
        if not table:
            raise DaiTableError('DataSource.read_df 需要表名（或直接传 sql=）')

        cols = '*'
        if columns:
            cols = ', '.join(columns if not isinstance(columns, str) else [columns])
        conds = _filter_sql(filters)
        extra = []
        if start_date:
            extra.append(f'{DEFAULT_PARTITION_FIELD} >= {_lit(start_date)}')
        if end_date:
            extra.append(f'{DEFAULT_PARTITION_FIELD} <= {_lit(end_date)}')
        if instruments:
            codes = ', '.join(_lit(x) for x in instruments)
            extra.append(f'instrument IN ({codes})')
        where = ' AND '.join([c for c in [conds] + extra if c])
        tail = f' WHERE {where}' if where else ''
        return _exec(f'SELECT {cols} FROM {table}{tail}')

    # -- 写（本地无平台存储）------------------------------------------
    def write_bdb(self, *args, **kwargs):
        """平台写 BDB；本地没有对应存储，空实现并返回 None。"""
        return None

    write_bdb_no_upload = write_bdb

    # -- 杂项 ----------------------------------------------------------
    def read_bdb(self, *args, **kwargs):
        raise DaiTableError(
            'read_bdb 读的是平台 BDB 存储，本地无此概念；'
            '请改用 DataSource().read_df(表名) 或 dai.query(sql)')


def DaiUDF(*args, **kwargs):  # noqa: N802 - 平台同名接口
    raise DaiTableError(
        'DaiUDF 是平台侧的服务端 UDF，本地无法执行；'
        '请把这段计算改写成 pandas/numpy 代码')


def install() -> types.ModuleType:
    """把本模块登记成 `dai` 与 `bigquant.dai`。"""
    mod = sys.modules.get(__name__)
    if mod is None:                     # 直接 python -c 调用时 __name__ 是 __main__
        mod = types.ModuleType('dai')
        for name in ('query', 'DataSource', 'DaiUDF', 'DEFAULT_PARTITION_FIELD',
                     'DaiTableError', 'available_tables'):
            setattr(mod, name, globals()[name])
    sys.modules['dai'] = mod
    return mod
