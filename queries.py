"""BigQuant 本地数据 —— DuckDB 查询接口（模拟盘本地数据层）
============================================================

本项目自带数据接口、不依赖任何外部目录。数据是本地 parquet 快照，
默认放在**本项目目录下**的 `data/`。

connect() 返回一个已建好视图的 duckdb 连接，视图惰性读取 parquet，
数据更新后无需重建，查询自动生效。

每张表会建两个视图：`v_<表名>`（主视图，惰性读 parquet）和 `<表名>`（裸名别名）。
裸名别名指向 v_ 视图，写 SQL 时直接用表名即可。

数据根目录（表子目录 cn_stock_real_bar1d/ 等的父目录）解析顺序：
    1. 环境变量 LOCALBQ_DATA_DIR（便于把数据放到别处）
    2. <本文件所在目录>/data      ← 独立分发包布局（C:\\smallquant\\data）
    3. <本文件上一级目录>/data    ← 原开发布局（项目目录与 data/ 并列）

用法：
    from queries import connect
    con = connect()
    con.execute(
        "SELECT * FROM cn_stock_real_bar1d WHERE instrument='000001.SZ' LIMIT 5"
    ).df()
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb

# 本文件所在目录（项目根）
_HERE = Path(__file__).resolve().parent
# 兼容旧引用：原布局里 queries.py 在子目录、data/ 在上一级
PROJECT_ROOT = _HERE.parent


def _default_data_root() -> Path:
    """数据根目录缺省值：优先本目录下的 data/（独立分发包），
    其次上一级 data/（原开发布局：data/ 与项目目录并列）。"""
    for cand in (_HERE / 'data', _HERE.parent / 'data'):
        if cand.is_dir():
            return cand
    return _HERE / 'data'


# 数据根目录：可用环境变量 LOCALBQ_DATA_DIR 覆盖
DATA_ROOT = Path(os.environ.get('LOCALBQ_DATA_DIR') or _default_data_root())

# data/ 目录内的表清单：name(视图后缀)、glob(相对数据根目录的 parquet 路径)、desc(说明)
TABLES = [
    dict(name="trading_days", glob="trading_days/*.parquet",
         desc="交易日历(按年,8 市场)"),
    dict(name="cn_stock_real_bar1d", glob="cn_stock_real_bar1d/*.parquet",
         desc="股票未复权日行情(按月,回测主力表)"),
    dict(name="cn_stock_bar1d", glob="cn_stock_bar1d/*.parquet",
         desc="股票后复权日行情(按月,算收益/技术指标)"),
    dict(name="cn_fund_real_bar1d", glob="cn_fund_real_bar1d/*.parquet",
         desc="基金未复权日线(按年)"),
    dict(name="cn_fund_bar1d", glob="cn_fund_bar1d/*.parquet",
         desc="基金后复权日行情(按年)"),
    dict(name="cn_stock_index_bar1d", glob="cn_stock_index_bar1d/*.parquet",
         desc="指数日行情(按年)"),
    dict(name="cn_stock_instruments", glob="cn_stock_instruments/*.parquet",
         desc="股票代码列表(含退市)"),
    dict(name="cn_stock_dividend", glob="cn_stock_dividend/*.parquet",
         desc="股票分红事件表(按年,date=除权除息日;送/转比例 bonus_rate/conversed_rate、"
         "每股红利 cash_before/after_tax,股权登记日 register_date)"),
    dict(name="cn_fund_instruments", glob="cn_fund_instruments/*.parquet",
         desc="基金代码列表"),
    dict(name="cn_stock_prefactors", glob="cn_stock_prefactors/*.parquet",
         desc="预计算因子大表(按月,2015 起,后复权,本地 151 列;云端 2026-09 更新字段集)",
         where="date IS NOT NULL",  # 导出时退市/无效代码各月含 1 行 date=NULL 占位,直接过滤
         ),
]


def parquet_files(t, root=None) -> list:
    """表目录下所有非空 parquet 文件。

    导出时可能留下 0 字节占位文件，直接扫会让 DuckDB 报
    "too small to be a Parquet file"，这里统一跳过。
    """
    pat = ((Path(root) if root else DATA_ROOT) / t["glob"]).as_posix()
    return sorted(
        f for f in Path(pat).parent.glob("*.parquet") if f.stat().st_size > 0
    )


def table_exists(t, root=None) -> bool:
    return len(parquet_files(t, root)) > 0


def read_sql(t, root=None) -> str:
    """生成读取该表 parquet 的 SQL(绝对路径,跳过空文件)。"""
    files = [f.as_posix() for f in parquet_files(t, root)]
    if not files:
        return "SELECT * FROM read_parquet([])"
    lst = ", ".join(f"'{f}'" for f in files)
    sql = f"SELECT * FROM read_parquet([{lst}])"
    if t.get("where"):
        sql += f" WHERE {t['where']}"
    return sql


def connect(root=None):
    """返回建好视图的 duckdb 连接(内存,惰性读 parquet,数据更新自动生效)。

    每张表建两个视图：v_<表名>(主视图)和 <表名>(裸名别名)，两种写法均可查询。
    root 缺省用模块级 DATA_ROOT；显式传入时以传入目录为数据根。
    """
    con = duckdb.connect()
    for t in TABLES:
        if table_exists(t, root):
            con.execute(f"CREATE OR REPLACE VIEW v_{t['name']} AS {read_sql(t, root)}")
            con.execute(f"CREATE OR REPLACE VIEW {t['name']} AS SELECT * FROM v_{t['name']}")
    return con


if __name__ == "__main__":
    con = connect()
    print(f"数据根目录: {DATA_ROOT}")
    print("可用视图:")
    for t in TABLES:
        exists = "✔" if table_exists(t) else "✘ 暂无数据"
        print(f"  v_{t['name']:<24} {t['desc']}  [{exists}]")
