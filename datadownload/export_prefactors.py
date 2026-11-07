#!/usr/bin/env python3
"""
cn_stock_prefactors 字段导出：BigQuant DAI → 本地 Parquet（按月分文件，统一预热）

用法：
    python scripts/export_prefactors.py                       # 默认 2015-01 ~ 2026-08
    python scripts/export_prefactors.py --start 2010-01 --end 2014-12
    python scripts/export_prefactors.py --force               # 覆盖已存在的月文件重导

目录拆分：
    2020 年（含）起的月份写入独立目录 data/cn_stock_prefactors_2020p/，
    2019 及以前在 data/cn_stock_prefactors/。obv 全历史重算跨两个目录一起读。

为什么统一预热 LOOKBACK_DAYS 天：
    cn_stock_prefactors 里的技术指标（sma_250/rsi_120/atr_14/kdj_.../obv 等）不是静态预
    存列，而是查询时在查询范围内动态计算的。具体有两类问题：

    1) m_lag 类窗口因子：按月切片查询时，窗口因子每月前 N 个交易日因回看不足为 null。
       统一把每段查询起点前推 LOOKBACK_DAYS 天，目标区间就能在完整窗口上计算。

    2) obv 累积型指标：无论预热多久，平台都会从查询范围起点重新累积
       （已验证：按月查询时每月首日 obv=±当日成交量），预热救不了。导出完成后会
       用本地全历史 close/volume 重算 obv 并写回。

    因此不需要维护"哪些字段是窗口因子"的白名单：全部字段统一按 CHUNK_MONTHS 个月
    一段、每段 [段首日 - LOOKBACK_DAYS, 段末日] 连续查询，裁剪后按月落盘。

    LOOKBACK_DAYS=500（≈340 个交易日）：覆盖 sma_250 所需 250 个交易日 + 约 90 个交易日
    的停牌缓冲。停牌超过 ~4 个月的股票长窗口因子仍有残余 null（A 股极少），必要时
    调大。

其他：
- 字段列表来自 /home/aiuser/work/prefactors_field_selected_final.csv
- 需要先登录：bq auth login 或 bq auth --apikey 'AK.SK'
- 全字段大段查询内存较高（500 天预热 + 6 个月 ≈ 22 个月全字段 ~1.7GB），
  内存吃紧时把 CHUNK_MONTHS 调小。

退出码：0=正常完成，1=单月文件超限停止，2=凭据失效（401/403）中止
"""
import argparse
import calendar
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

TABLE = "cn_stock_prefactors"
SPLIT_YEAR = 2020        # 2020 年（含）起的月份写入独立目录
PRE_DIR = DATA_DIR / TABLE
PRE_DIR_2020P = DATA_DIR / f"{TABLE}_{SPLIT_YEAR}p"
FIELD_FILE = Path("/home/aiuser/work/prefactors_field_selected_final.csv")
LOOKBACK_DAYS = 500   # 统一预热天数，覆盖 sma_250 + 中等停牌缓冲
CHUNK_MONTHS = 6      # 每段覆盖的有用月份数，内存吃紧时调小
MAX_MB = 100          # 单月 parquet 超过该大小则停止
MAX_BYTES = MAX_MB * 1024 * 1024


def load_fields() -> list[str]:
    """读取字段筛选 CSV（跳过 BOM 与空行）"""
    fields = [
        line.strip()
        for line in FIELD_FILE.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if not fields:
        raise SystemExit(f"[!!] 字段文件为空: {FIELD_FILE}")
    if len(set(fields)) != len(fields):
        raise SystemExit("[!!] 字段列表存在重复")
    return fields


def first_last_day(ym: str) -> tuple[str, str]:
    """'202101' → (该月首日, 该月末日)，如 ('2021-01-01', '2021-01-31')"""
    year, month = int(ym[:4]), int(ym[4:6])
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


def out_dir_for(ym: str) -> Path:
    """'YYYYMM' → 所属输出目录：2020 年（含）起在独立目录"""
    return PRE_DIR_2020P if int(ym[:4]) >= SPLIT_YEAR else PRE_DIR


def iter_months(start: str, end: str):
    """'2021-01' ~ '2026-08' 逐月产出 'YYYYMM'"""
    y, m = int(start[:4]), int(start[5:7])
    end_y, end_m = int(end[:4]), int(end[5:7])
    while (y, m) <= (end_y, end_m):
        yield f"{y:04d}{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def chunk_months(months: list[str], size: int):
    """把月份列表切成连续 chunk，产出 (chunk_首月, chunk_末月, 月份列表)"""
    for i in range(0, len(months), size):
        part = months[i : i + size]
        yield part[0], part[-1], part


def is_auth_error(e: Exception) -> bool:
    """判断异常是否为凭据失效（401/403）"""
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status in (401, 403):
        return True
    text = str(e)
    return "401 Unauthorized" in text or "403 Forbidden" in text


def warn_token_expiry() -> None:
    """凭据为会话 token 且临近过期（<1 天）时提前提醒"""
    cfg = Path.home() / ".bigquant" / "config.json"
    try:
        import base64
        import json

        token = json.loads(cfg.read_text(encoding="utf-8")).get("auth", {}).get("token", "")
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if not exp:
            return
        left = exp - time.time()
        if left <= 0:
            hint = "已过期"
        elif left < 86400:
            hint = f"仅剩 {left / 3600:.1f} 小时"
        else:
            return
        print(
            f"[warn] BigQuant 会话 token {hint}，导出中途可能因 401 失败。\n"
            f"       建议改用长期凭据：bq auth --apikey 'AK.SK'",
            flush=True,
        )
    except Exception:
        pass


def ensure_auth() -> None:
    """读取凭据并校验，401/403 时给出明确提示后退出"""
    global dai
    from bigquant import dai, init_from_config

    try:
        init_from_config()
    except Exception as e:
        if is_auth_error(e):
            raise SystemExit(
                f"[!!] BigQuant 凭据失效或已过期：{e}\n"
                f"     请重新登录后再运行：\n"
                f"       bq auth --apikey 'AK.SK'      # 推荐，长期有效\n"
                f"       bq auth login                 # 会话 token 约 7 天有效\n"
                f"     登录后可用 bq auth whoami 验证。"
            )
        raise


def query_df(sql: str, filters: dict, tag: str, retries: int = 3):
    """执行查询并返回 pandas df；401/403 时抛 SystemExit(2)"""
    for attempt in range(1, retries + 1):
        try:
            t0 = time.time()
            df = dai.query(sql, filters=filters).df()
            print(f"  [ok] {tag} 查询 {len(df)} 行，用时 {time.time() - t0:.0f}s", flush=True)
            return df
        except Exception as e:
            print(f"  [!!] {tag} 第{attempt}次失败: {e}", flush=True)
            if is_auth_error(e):
                print(
                    "\n[!!] 凭据失效（401/403），停止导出。\n"
                    "     请执行 bq auth --apikey 'AK.SK' 或 bq auth login 后重跑（已完成部分自动跳过）。",
                    flush=True,
                )
                raise SystemExit(2)
            time.sleep(3 * attempt)
    raise SystemExit(f"[!!] {tag} 重试{retries}次仍失败")


def write_month_files(df, part: list[str], force: bool = False) -> tuple[int, int]:
    """把查询结果裁到 part 覆盖的月份，逐月写 <ym>.parquet（原子）。
    返回 (写入数, 跳过数)。"""
    import pandas as pd
    import polars as pl

    start_d, _ = first_last_day(part[0])
    _, end_d = first_last_day(part[-1])
    df = df.copy()
    if not pd.api.types.is_datetime64_any_dtype(df["date"].dtype):
        df["date"] = pd.to_datetime(df["date"])

    lo, hi = pd.Timestamp(start_d), pd.Timestamp(end_d)
    keep = df[(df["date"] >= lo) & (df["date"] <= hi)]
    if keep.empty:
        return 0, 0
    keep = keep.copy()
    keep["_ym"] = keep["date"].dt.strftime("%Y%m")

    n_done = n_skip = 0
    for ym, grp in keep.groupby("_ym", sort=True):
        out_dir = out_dir_for(ym)
        out_dir.mkdir(parents=True, exist_ok=True)
        grp = grp.drop(columns=["_ym"])
        out_file = out_dir / f"{ym}.parquet"
        if out_file.exists() and out_file.stat().st_size > 0 and not force:
            n_skip += 1
            continue
        tmp = out_dir / f"{ym}.parquet.tmp"
        pl.from_pandas(grp).write_parquet(tmp)
        os.replace(tmp, out_file)
        if out_file.stat().st_size > MAX_BYTES:
            raise SystemExit(f"[!!] {out_file} 超过 {MAX_MB}MB，停止导出")
        n_done += 1
    return n_done, n_skip


def recalc_obv() -> None:
    """用本地全历史 close/volume 重算 obv 并写回。

    obv 是累积型指标，平台在查询时会从查询范围起点重新累积，无论预热多久都没法
    通过分段/按月查询拿到正确的全历史累积值。已验证：按月查询时每月首日 obv=±当日
    成交量；月内逐日与下式对拍一致（含停牌行）：

        step_t = sign(close_t - close_{t-1}) * volume_t   (停牌/无行情行 step=0)
        obv_t  = sum_{s<=t} step_s                        (首日 sign 视为 +1)

    幂等：重算结果只取决于本地数据，结果一致。补导更早月份后需再次运行。

    2020 拆目录后：全历史读取跨 data/cn_stock_prefactors/ 与 _2020p/ 两个目录，
    写回仍落到各文件自己的目录。
    """
    import polars as pl

    files = []
    for d in (PRE_DIR, PRE_DIR_2020P):
        files.extend(sorted(d.glob("*.parquet")))
    files = [f for f in files if re.fullmatch(r"\d{6}[ab]?\.parquet", f.name)]
    if not files:
        return

    sample = pl.read_parquet(files[0], n_rows=0)
    if "obv" not in sample.columns:
        return

    print(f"\n=== {TABLE} 重算 obv（累积型，按全历史累积）===")
    cols = ["date", "instrument", "close", "volume"]
    big = pl.concat([pl.read_parquet(f, columns=cols) for f in files]).sort(["instrument", "date"])
    big = big.with_columns(pl.col("close").forward_fill().over("instrument").alias("_cff"))
    big = big.with_columns(
        pl.when(pl.col("close").is_null() | pl.col("volume").is_null())
        .then(0.0)
        .otherwise(pl.col("_cff").diff().over("instrument").sign().fill_null(1.0) * pl.col("volume"))
        .alias("_step")
    ).with_columns(pl.col("_step").cum_sum().over("instrument").alias("obv"))
    big = big.with_columns(pl.col("date").dt.strftime("%Y%m").alias("_ym")).drop("_cff", "_step")

    n = 0
    for f in files:
        ym = f.stem[:6]
        new = big.filter(pl.col("_ym") == ym).select(["date", "instrument", "obv"])
        if new.height == 0:
            continue
        cur = pl.read_parquet(f)
        if cur.height == 0:
            continue
        merged = cur.drop("obv").join(new, on=["date", "instrument"], how="left")
        merged = merged.select(cur.columns)
        tmp = f.parent / f"{ym}.parquet.tmp"
        merged.write_parquet(tmp)
        os.replace(tmp, f)
        n += 1
    print(f"[done] obv 重算写回 {n} 个月")


def main():
    import log_util
    log_util.setup_logging()
    parser = argparse.ArgumentParser(description="cn_stock_prefactors DAI → Parquet 导出（统一预热取数）")
    parser.add_argument("--start", default="2015-01", help="起始月份 YYYY-MM，默认 2015-01")
    parser.add_argument("--end", default="2026-08", help="结束月份 YYYY-MM（含），默认 2026-08")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的月文件重导（默认跳过）")
    args = parser.parse_args()

    fields = load_fields()
    keys = [f for f in ("date", "instrument") if f in fields]
    cols = keys + [f for f in fields if f not in keys]
    print(f"字段数: {len(fields)}")

    months = list(iter_months(args.start, args.end))
    print(
        f"月份范围: {months[0]} ~ {months[-1]}，共 {len(months)} 个月 | "
        f"每 {CHUNK_MONTHS} 个月一段、每段预热 {LOOKBACK_DAYS} 天"
    )
    print(f"输出目录: <{SPLIT_YEAR} 年 {PRE_DIR} | {SPLIT_YEAR} 年(含)起 {PRE_DIR_2020P}")

    warn_token_expiry()
    ensure_auth()

    select = ", ".join(cols)
    print(f"\n=== {TABLE} 统一预热导出 ===")
    for m1, m2, part in chunk_months(months, CHUNK_MONTHS):
        start_d, _ = first_last_day(part[0])
        _, end_d = first_last_day(part[-1])
        q_start = (
            datetime.strptime(start_d, "%Y-%m-%d") - timedelta(days=LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")
        df = query_df(
            f"SELECT {select} FROM {TABLE}",
            {"date": [q_start, end_d]},
            f"{part[0]}~{part[-1]}",
        )
        if df.empty:
            continue
        n_done, n_skip = write_month_files(df, part, force=args.force)
        print(f"       {part[0]}~{part[-1]}: {n_done} 个月写入，{n_skip} 个已存在跳过", flush=True)
        del df

    recalc_obv()
    print(f"\n[done] {TABLE}: 导出完成")


if __name__ == "__main__":
    main()
