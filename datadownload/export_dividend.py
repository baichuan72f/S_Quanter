#!/usr/bin/env python3
"""
分红信息（股票/基金）按年导出：BigQuant DAI → 本地 Parquet

用法：
    python scripts/export_dividend.py                                        # 股票分红 2015 ~ 2026，全量
    python scripts/export_dividend.py --table cn_fund_dividend               # 基金分红 2015 ~ 2026，全量
    python scripts/export_dividend.py --table cn_fund_dividend --start-year 2010 --end-year 2026
    python scripts/export_dividend.py --start-year 2015 --end-year 2024

支持表：
- cn_stock_dividend（默认）：股票分红，10 列：date, instrument, report_date,
  publish_date, bonus_rate, conversed_rate, cash_before_tax, cash_after_tax,
  register_date, ex_date
- cn_fund_dividend：基金分红，7 列：instrument, name, date, register_date,
  cash_dividend, dividend_distribution_date, fund_type

说明：
- 事件表，数据量小（每年约几千行），直接按年查询，输出到
  data/<表名>/<YYYY>.parquet（zstd 压缩），无需按月再合并
- 按 date 切片：date 实际为**除权除息日**（股票与行情表交易日对齐，基金为除息日），
  公告/派发落在哪个除息年就归入哪个文件
- 已存在的年度文件自动跳过（断点续传）
- 需要先登录：bq auth login 或 bq auth --apikey 'AK.SK'
"""
import argparse
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# 支持的表：DAI 表名 → 中文名（用于日志）
DIVIDEND_TABLES = {
    "cn_stock_dividend": "股票分红",
    "cn_fund_dividend": "基金分红",
}


def export_year(table: str, year: int, retries: int = 3) -> None:
    """按年导出单年数据"""
    out_dir = DATA_DIR / table
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{year}.parquet"
    if out_file.exists() and out_file.stat().st_size > 0:
        print(f"  [skip] {table}/{year}.parquet 已存在（断点续传）")
        return
    start_d, end_d = f"{year}-01-01", f"{year}-12-31"
    sql = f"SELECT * FROM {table}"
    filters = {"date": [start_d, end_d]}
    for attempt in range(1, retries + 1):
        try:
            df = dai.query(sql, filters=filters).df()
            if df.empty:
                print(f"  [empty] {year}: 无数据（不写文件）")
                return
            pl.from_pandas(df).write_parquet(out_file)
            size_mb = out_file.stat().st_size / (1024 * 1024)
            print(f"  [ok] {table}/{year}.parquet  rows={len(df)}  size={size_mb:.2f}MB", flush=True)
            return
        except Exception as e:
            print(f"  [!!] {year} 第{attempt}次失败: {e}", flush=True)
            time.sleep(3 * attempt)
    print(f"  [!!] {year} 重试{retries}次仍失败，跳过", flush=True)


def main():
    import log_util
    log_util.setup_logging()
    parser = argparse.ArgumentParser(description="分红信息（股票/基金）DAI → Parquet 按年导出")
    parser.add_argument(
        "--table",
        default="cn_stock_dividend",
        choices=list(DIVIDEND_TABLES),
        help=f"要导出的表，默认 cn_stock_dividend: {list(DIVIDEND_TABLES)}",
    )
    parser.add_argument("--start-year", type=int, default=2015, help="起始年份，默认 2015")
    parser.add_argument("--end-year", type=int, default=2026, help="结束年份（含），默认 2026")
    args = parser.parse_args()
    if args.end_year < args.start_year:
        raise SystemExit("[!!] --end-year 不能小于 --start-year")
    table = args.table
    print(f"表: {table}（{DIVIDEND_TABLES[table]}），年份范围: {args.start_year} ~ {args.end_year}")

    # 延迟导入，避免未登录时 import 失败
    global dai, pl
    from bigquant import dai, init_from_config

    # 从 ~/.bigquant/config.json 读取凭据（bq auth login / bq auth --apikey 生成）
    init_from_config()
    import polars as pl

    for year in range(args.start_year, args.end_year + 1):
        print(f"=== {table}/{year} ===")
        export_year(table, year)


if __name__ == "__main__":
    sys.exit(main())
