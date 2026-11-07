#!/usr/bin/env python3
"""
P0 八张表数据导出脚本：BigQuant DAI → 本地 Parquet（按月分文件）

用法：
    python scripts/export_p0.py                          # 全量（默认 2021-01 起）
    python scripts/export_p0.py --start 2021-01 --end 2026-08
    python scripts/export_p0.py --tables cn_stock_real_bar1d

说明：
- 每张表按自然月分批查询，输出到 data/<表名>/<YYYYMM>.parquet
- 已存在的月份文件自动跳过（断点续传）
- 查询结果经 pandas → polars 写入 parquet（zstd 压缩）
- 需要先登录：bq auth login 或 bq auth --apikey 'AK.SK'
"""
import argparse
import calendar
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# P0 八张表：DAI 表名 → 中文名（用于日志）
P0_TABLES = {
    "trading_days": "交易日历",
    "cn_stock_instruments": "股票代码列表",
    "cn_fund_instruments": "基金代码列表",
    "cn_stock_real_bar1d": "股票未复权日行情",
    "cn_stock_bar1d": "股票后复权日行情",
    "cn_fund_real_bar1d": "基金未复权日线",
    "cn_fund_bar1d": "基金后复权日行情",
    "cn_stock_index_bar1d": "指数日行情",
}


def month_range(ym: str) -> tuple[str, str]:
    """'202101' → (该月首日, 该月末日)，如 ('2021-01-01', '2021-01-31')"""
    year, month = int(ym[:4]), int(ym[4:6])
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


def iter_months(start: str, end: str):
    """'2021-01' ~ '2026-08' 逐月产出 'YYYYMM'"""
    y, m = int(start[:4]), int(start[5:7])
    end_y, end_m = int(end[:4]), int(end[5:7])
    while (y, m) <= (end_y, end_m):
        yield f"{y:04d}{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def export_table(table: str, months: list[str], retries: int = 3) -> None:
    """按月导出单张表"""
    out_dir = DATA_DIR / table
    out_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    for ym in months:
        out_file = out_dir / f"{ym}.parquet"
        if out_file.exists() and out_file.stat().st_size > 0:
            done += 1
            continue
        start_d, end_d = month_range(ym)
        sql = f"SELECT * FROM {table}"
        filters = {"date": [start_d, end_d]}
        ok = False
        for attempt in range(1, retries + 1):
            try:
                df = dai.query(sql, filters=filters).df()
                if df.empty:
                    # 空月份也写一个空文件占位，避免重复查询
                    pl.DataFrame(schema=df.dtypes).write_parquet(out_file)
                else:
                    pl.from_pandas(df).write_parquet(out_file)
                done += 1
                print(f"  [ok] {table}/{ym}.parquet  rows={len(df)}")
                ok = True
                break
            except Exception as e:
                print(f"  [!!] {table}/{ym} 第{attempt}次失败: {e}", flush=True)
                time.sleep(3 * attempt)
        if not ok:
            print(f"  [!!] {table}/{ym} 重试{retries}次仍失败，跳过", flush=True)
    print(f"[done] {table}: {done}/{len(months)} 个月")


def main():
    import log_util
    log_util.setup_logging()
    parser = argparse.ArgumentParser(description="P0 表 DAI → Parquet 导出")
    parser.add_argument("--start", default="2015-01", help="起始月份 YYYY-MM，默认 2015-01")
    parser.add_argument("--end", default="2026-08", help="结束月份 YYYY-MM（含）")
    parser.add_argument(
        "--tables",
        nargs="+",
        default=list(P0_TABLES),
        help=f"要导出的表，默认全部: {list(P0_TABLES)}",
    )
    args = parser.parse_args()

    months = list(iter_months(args.start, args.end))
    print(f"月份范围: {months[0]} ~ {months[-1]}，共 {len(months)} 个月")

    # 延迟导入，避免未登录时 import 失败
    global dai, pl
    from bigquant import dai, init_from_config

    # 从 ~/.bigquant/config.json 读取凭据（bq auth login 生成）
    init_from_config()
    import polars as pl

    for table in args.tables:
        if table not in P0_TABLES:
            print(f"[skip] 未知表: {table}（不在 P0 列表）")
            continue
        print(f"\n=== {table} ({P0_TABLES[table]}) ===")
        export_table(table, months)


if __name__ == "__main__":
    main()
