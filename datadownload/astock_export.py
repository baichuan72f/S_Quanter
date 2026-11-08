"""A 股数据导出 —— 从公开源（通达信官网盘后包 / 深交所日历 / 东财快照与分红）生成
与本地快照同布局的 parquet，供 `queries.py` 直接当数据根使用。

用法：
    python astock_export.py --root /home/agent/data/smallquant/data_astock --start 2026-10-09 --end 2026-11-06
    python astock_export.py --root ... --date 2026-11-06          # 单日增量

写出的表（列名/类型对齐现有 BigQuant 快照）：
    trading_days/YYYY.parquet            date, market_code='CN'
    cn_stock_real_bar1d/YYYYMM.parquet   真实价日线（引擎回测主表）
    cn_stock_bar1d/YYYYMM.parquet        后复权日线（真实价 × adjust_factor）
    cn_stock_instruments/YYYY.parquet    date, instrument, name, type='stock'
    cn_stock_dividend/YYYY.parquet       分红送转（每股口径，date=除权除息日）
    cn_stock_prefactors/YYYYMM.parquet   因子子集（close 后复权、总/流通市值、换手率、ST 状态等）

口径
    1. 真实价来自通达信盘后包（volume=股、amount=元），与 BigQuant 快照逐日对拍一致。
    2. 总股本/流通股本由东财快照反推（总市值/现价、流通市值/现价），再乘当日真实收盘价得到当日市值；
       快照是当日实时值，股本变化很慢，用它乘历史收盘是常规做法。
    3. 换手率 = 成交量 / 流通股本。
    4. 后复权因子由窗口内已知分红递推：最新一日为 1，向历史累乘（送转 + 现金红利/昨收）。
       窗口之前的分红不在可查范围，跨越窗口起点的更早历史会偏小——本仓只保留近月快照。
    5. ST 状态按名称：*ST → 2，ST → 1，其余 0（与 BigQuant 的 st_status 编码一致）。
"""
from __future__ import annotations

import argparse
import io
import json
import math
import re
import struct
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) astock-export/1.0"
TDX_URL = "https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip"
TDX_MIN_PRICED = {"sh": 10000, "sz": 3000, "bj": 50}
SZSE_CALENDAR = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month={month}"
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
EM_BONUS = "https://datacenter-web.eastmoney.com/api/data/v1/get"

EM_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
EM_FIELDS = "f2,f8,f12,f13,f14,f20,f21"

# A 股股票代码段（与 BigQuant cn_stock_instruments 的 5,632 只逐个比对后确定）
STOCK_RULE = (
    "(instrument LIKE '600%.SH' OR instrument LIKE '601%.SH' OR instrument LIKE '603%.SH'"
    " OR instrument LIKE '605%.SH' OR instrument LIKE '688%.SH' OR instrument LIKE '689%.SH'"
    " OR instrument LIKE '000%.SZ' OR instrument LIKE '001%.SZ' OR instrument LIKE '002%.SZ'"
    " OR instrument LIKE '003%.SZ' OR instrument LIKE '300%.SZ' OR instrument LIKE '301%.SZ'"
    " OR instrument LIKE '302%.SZ'"
    " OR instrument LIKE '43%.BJ' OR instrument LIKE '83%.BJ' OR instrument LIKE '87%.BJ'"
    " OR instrument LIKE '88%.BJ' OR instrument LIKE '920%.BJ')"
)


def is_stock(instrument: str) -> bool:
    """同一规则在 python 侧的判定（写表前过滤，省得每张表都带 SQL 条件）。"""
    code, _, market = instrument.partition(".")
    return bool(
        (market == "SH" and code[:3] in {"600", "601", "603", "605", "688", "689"})
        or (market == "SZ" and code[:3] in {"000", "001", "002", "003", "300", "301", "302"})
        or (market == "BJ" and code[:2] in {"43", "83", "87", "88"} or (market == "BJ" and code[:3] == "920"))
    )


def st_status_of(name: str) -> float:
    if name.startswith("*ST"):
        return 2.0
    if name.startswith("ST"):
        return 1.0
    return 0.0


# ── 网络：国内源一律不走代理（本机代理会掐断这些站的 TLS） ────────────────
def _open(url: str, timeout: float = 90) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": url})
    with opener.open(request, timeout=timeout) as response:
        return response.read()


def _json(url: str, timeout: float = 40) -> dict:
    return json.loads(_open(url, timeout).decode("utf-8"))


def tdx_package(date: str) -> list[dict]:
    """通达信官网盘后包：某交易日沪深北全部证券的日线（含成交额）。"""
    ymd = date.replace("-", "")
    try:
        content = _open(TDX_URL.format(ymd=ymd), timeout=120)
    except Exception as exc:
        raise ValueError(f"{date} 取不到通达信盘后包：{type(exc).__name__}: {exc}") from exc
    if not content.startswith(b"PK"):
        raise RuntimeError(f"通达信盘后包 {date} 不是 zip（可能返回了错误页）")
    archive = zipfile.ZipFile(io.BytesIO(content))
    names = set(archive.namelist())
    rows: list[dict] = []
    for market in ("sh", "sz", "bj"):
        cod_name, md1_name = f"{market}{ymd[2:]}.cod", f"{market}{ymd[2:]}.md1"
        if cod_name not in names or md1_name not in names:
            if market == "bj" and ymd < "20220506":
                continue
            raise RuntimeError(f"盘后包 {date} 缺少 {cod_name}/{md1_name}")
        cod, md1 = archive.read(cod_name), archive.read(md1_name)
        if len(cod) % 150 or len(md1) % 512 or len(cod) // 150 != len(md1) // 512:
            raise RuntimeError(f"盘后包 {date} 的 {market} 代码表/行情块长度对不上")
        before = len(rows)
        for offset in range(0, len(cod), 150):
            record = cod[offset:offset + 150]
            code = record[0:6].rstrip(b"\x00 ").decode("ascii", "replace")
            seq = struct.unpack("<H", record[32:34])[0]
            if not re.fullmatch(r"[0-9]{6}", code):
                raise RuntimeError(f"盘后包 {date} {market} 出现非 6 位代码 {code!r}")
            block = md1[seq * 512:(seq + 1) * 512]
            if len(block) != 512:
                raise RuntimeError(f"盘后包 {date} {market}{code} 行情块越界")
            prev_close = struct.unpack("<d", block[4:12])[0]
            open_, high, low, close = struct.unpack("<4d", block[12:44])
            amount = struct.unpack("<d", block[72:80])[0]
            if not all(math.isfinite(v) for v in (prev_close, open_, high, low, close, amount)):
                raise RuntimeError(f"盘后包 {date} {market}{code} 出现非有限数值")
            if close <= 0:
                continue
            volume = struct.unpack("<Q", block[56:64])[0]
            name = record[40:72].split(b"\x00")[0].decode("gbk").strip()
            rows.append({
                "date": date, "instrument": f"{code}.{market.upper()}", "name": name,
                "pre_close": prev_close, "open": open_, "high": high, "low": low,
                "close": close, "volume": int(volume), "amount": amount,
            })
        if len(rows) - before < TDX_MIN_PRICED[market]:
            raise RuntimeError(f"盘后包 {date} {market} 只有 {len(rows) - before} 条有价记录，怀疑残缺")
    return rows


def trading_days_of_month(year: int, month: int) -> list[str]:
    payload = _json(SZSE_CALENDAR.format(month=f"{year}-{month}"))
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"深交所没有返回 {year}-{month} 的日历")
    days = [str(rec["jyrq"])[:10] for rec in data if str(rec.get("jybz")) == "1"]
    if not days:
        raise RuntimeError(f"深交所 {year}-{month} 没有交易日")
    return sorted(days)


def snapshot() -> dict[str, dict]:
    """东财全市场快照：名称 / 现价 / 总市值 / 流通市值 → 反推总股本与流通股本。"""
    rows: list[dict] = []
    page, total = 1, None
    while True:
        query = urllib.parse.urlencode({
            "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f12", "fs": EM_FS, "fields": EM_FIELDS,
        })
        payload = _json(f"{EM_CLIST}?{query}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or payload.get("rc") != 0 or not data:
            raise RuntimeError(f"东财 clist 返回异常：{str(payload)[:120]}")
        page_total = int(data["total"])
        if total is None:
            total = page_total
        elif page_total != total:
            raise RuntimeError(f"东财翻页途中 total 从 {total} 变成 {page_total}，重试")
        rows.extend(data["diff"])
        if len(rows) >= total:
            break
        page += 1
    if len(rows) != total:
        raise RuntimeError(f"东财快照 {len(rows)} 条与 total={total} 不符")
    out: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("f12") or "").strip()
        if not re.fullmatch(r"[0-9]{6}", code):
            continue
        market = {0: "SZ", 1: "SH"}.get(row.get("f13"))
        if market is None:
            market = "BJ" if code[:2] in {"43", "83", "87", "88"} or code[:3] == "920" else None
        if market is None:
            continue
        price = row.get("f2")
        total_cap, float_cap = row.get("f20"), row.get("f21")
        usable = isinstance(price, (int, float)) and price and price > 0
        out[f"{code}.{market}"] = {
            "name": str(row.get("f14") or "").strip(),
            "price": price if usable else None,
            "total_shares": (total_cap / price) if usable and total_cap else None,
            "float_shares": (float_cap / price) if usable and float_cap else None,
        }
    return out


def dividends(start: str, end: str) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({
            "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
            "pageNumber": page, "pageSize": 500,
            "sortColumns": "EX_DIVIDEND_DATE", "sortTypes": "1",
            "filter": f"(EX_DIVIDEND_DATE>='{start}')(EX_DIVIDEND_DATE<='{end}')",
        })
        payload = _json(f"{EM_BONUS}?{query}")
        result = payload.get("result") or {}
        data = result.get("data") or []
        for row in data:
            code = str(row.get("SECURITY_CODE") or "").strip()
            secucode = str(row.get("SECUCODE") or "").upper()
            suffix = "SH" if secucode.endswith(".SH") else "SZ" if secucode.endswith(".SZ") else "BJ"
            ex_date = str(row.get("EX_DIVIDEND_DATE") or "")[:10]
            if not re.fullmatch(r"[0-9]{6}", code) or not ex_date:
                continue
            rows.append({
                "date": ex_date, "instrument": f"{code}.{suffix}",
                "report_date": str(row.get("REPORT_DATE") or "")[:10] or None,
                "publish_date": str(row.get("NOTICE_DATE") or "")[:10] or None,
                "bonus_rate": (row.get("BONUS_RATIO") or 0) / 10,
                "conversed_rate": (row.get("TRANSFER_RATIO") or 0) / 10,
                "cash_before_tax": (row.get("PRETAX_BONUS_RMB") or 0) / 10,
                "cash_after_tax": (row.get("PRETAX_BONUS_RMB") or 0) / 10,
                "register_date": str(row.get("EQUITY_RECORD_DATE") or "")[:10] or None,
                "ex_date": ex_date,
            })
        pages = int(result.get("pages") or 1)
        if page >= pages or not data:
            break
        page += 1
    return rows


def build_adjust(days: list[str], bars: dict[str, list[dict]], events: dict[str, list[dict]]) -> dict[str, dict[str, float]]:
    """{date: {instrument: factor}}，bar = real × factor；最新一日 factor=1，向历史按分红累乘。"""
    by_day: dict[str, list[dict]] = {}
    for rows in events.values():
        for event in rows:
            by_day.setdefault(event["date"], []).append(event)

    out: dict[str, dict[str, float]] = {}
    state: dict[str, float] = {}
    for day in sorted(days, reverse=True):
        factors = {row["instrument"]: state.get(row["instrument"], 1.0) for row in bars.get(day, [])}
        out[day] = factors
        for event in by_day.get(day, []):
            row = next((item for item in bars.get(day, []) if item["instrument"] == event["instrument"]), None)
            if row is None or not row["pre_close"] or row["pre_close"] <= 0:
                continue
            base = state.get(event["instrument"], 1.0)
            share = 1.0 + (event["bonus_rate"] or 0) + (event["conversed_rate"] or 0)
            cash = (event["cash_before_tax"] or 0) / row["pre_close"]
            state[event["instrument"]] = base * (share + cash)
    return out


def _stringify(frame: pd.DataFrame) -> pd.DataFrame:
    """把 object 列统一成字符串。

    旧快照里 `report_date` 这类列是整数（20260930），新导出是 'YYYY-MM-DD'，
    pyarrow 面对混合 int/str 的 object 列会按样本推断成 int 然后炸掉。
    这份数据是给人读的、也只按值 join，统一成字符串最稳。
    """
    out = frame.copy()
    for column in out.columns:
        if out[column].dtype == object:
            out[column] = out[column].map(lambda value: None if pd.isna(value) else str(value))
    return out


def _align(old: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """把两边同名但类型不同的列统一成 object：旧文件可能把日期存成 int，新数据是字符串，pyarrow 拼接会直接报错。"""
    for column in set(old.columns) & set(new.columns):
        if old[column].dtype != new[column].dtype:
            old[column] = old[column].astype(object)
            new[column] = new[column].astype(object)
    return old, new


def write_month(rows: list[dict], root: Path, table: str) -> None:
    frame = pd.DataFrame(rows)
    if frame.empty:
        print(f"[warn] {table} 无数据")
        return
    frame["date"] = pd.to_datetime(frame["date"])
    for month, group in frame.groupby(frame["date"].dt.strftime("%Y%m")):
        path = root / table / f"{month}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            old = pd.read_parquet(path)
            old["date"] = pd.to_datetime(old["date"])
            keep = old[~old.set_index(["date", "instrument"]).index.isin(group.set_index(["date", "instrument"]).index)]
            keep, group = _align(keep, group)
            group = pd.concat([keep, group])
        group.sort_values(["date", "instrument"]).pipe(_stringify).to_parquet(path, index=False)
        print(f"[write] {path} {len(group)} 行")


def write_year(rows: list[dict], root: Path, table: str) -> None:
    frame = pd.DataFrame(rows)
    if frame.empty:
        print(f"[warn] {table} 无数据")
        return
    frame["date"] = pd.to_datetime(frame["date"])
    for year, group in frame.groupby(frame["date"].dt.year):
        path = root / table / f"{year}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            old = pd.read_parquet(path)
            old["date"] = pd.to_datetime(old["date"])
            keep = old[~old.set_index(["date", "instrument"]).index.isin(group.set_index(["date", "instrument"]).index)]
            keep, group = _align(keep, group)
            group = pd.concat([keep, group])
        group.sort_values(["date", "instrument"]).pipe(_stringify).to_parquet(path, index=False)
        print(f"[write] {path} {len(group)} 行")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--date")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    root = Path(args.root)

    if args.date:
        days = [args.date]
    else:
        if not args.start or not args.end:
            raise SystemExit("要么给 --date，要么给 --start 与 --end")
        months = set()
        cursor = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
        while cursor <= end:
            months.add((cursor.year, cursor.month))
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        days = sorted(day for year, month in months for day in trading_days_of_month(year, month)
                      if args.start <= day <= args.end)
    print(f"[plan] {len(days)} 个交易日：{days[0]} … {days[-1]}")

    for year in sorted({day[:4] for day in days}):
        wanted = set(days)
        year_days = sorted({d for day in days for d in trading_days_of_month(int(year), int(day[5:7]))
                            if d.startswith(year) and (len(days) > 20 or d in wanted)})
        table = pd.DataFrame({"date": pd.to_datetime(year_days), "market_code": "CN"})
        path = root / "trading_days" / f"{year}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        _stringify(table).to_parquet(path, index=False)
        print(f"[calendar] {path} {len(table)} 行")

    bars: dict[str, list[dict]] = {}
    for day in days:
        try:
            rows = [row for row in tdx_package(day) if is_stock(row["instrument"])]
            bars[day] = rows
            print(f"[bars] {day} {len(rows)} 只")
        except ValueError as exc:
            print(f"[skip] {exc}")
    if not bars:
        raise SystemExit("没有任何交易日的盘后包可用")

    events_raw = dividends(min(bars), max(bars))
    events: dict[str, list[dict]] = {}
    for row in events_raw:
        events.setdefault(row["instrument"], []).append(row)
    for rows_of_code in events.values():
        rows_of_code.sort(key=lambda item: item["date"])
    print(f"[dividend] {len(events_raw)} 条事件，覆盖 {len(events)} 只")

    factors = build_adjust(list(bars), bars, events)
    snap = snapshot()
    print(f"[snapshot] {len(snap)} 只标的")

    real_rows, adj_rows, pref_rows, inst_rows = [], [], [], []
    shares: dict[str, tuple[float | None, float | None]] = {}
    for day, rows in bars.items():
        for row in rows:
            code = row["instrument"]
            factor = factors.get(day, {}).get(code, 1.0)
            info = snap.get(code, {})
            total_shares, float_shares = shares.get(code, (None, None))
            if total_shares is None and info.get("total_shares"):
                total_shares = info["total_shares"]
            if float_shares is None and info.get("float_shares"):
                float_shares = info["float_shares"]
            shares[code] = (total_shares, float_shares)
            close = row["close"]
            base = {
                "date": pd.Timestamp(day), "instrument": code,
                "name": row["name"] or info.get("name") or "",
                "adjust_factor": factor,
                "pre_close": row["pre_close"], "open": row["open"], "close": close,
                "high": row["high"], "low": row["low"], "volume": row["volume"],
                "deal_number": 0, "amount": row["amount"],
                "change_ratio": (close / row["pre_close"] - 1) if row["pre_close"] else None,
                "turn": (row["volume"] / float_shares) if float_shares else None,
                "upper_limit": None, "lower_limit": None,
            }
            real_rows.append(dict(base))
            adjusted = dict(base)
            for column in ("pre_close", "open", "close", "high", "low"):
                adjusted[column] = base[column] * factor
            adj_rows.append(adjusted)
            pref_rows.append({
                "date": pd.Timestamp(day), "instrument": code, "name": base["name"],
                "close": close * factor,
                "total_market_cap": total_shares * close if total_shares else None,
                "float_market_cap": float_shares * close if float_shares else None,
                "turn": base["turn"],
                "st_status": st_status_of(base["name"]),
                "adjust_factor": factor, "total_shares": total_shares,
                "is_sh": 1.0 if code.endswith(".SH") else 0.0,
                "is_sz": 1.0 if code.endswith(".SZ") else 0.0,
                "is_bj": 1.0 if code.endswith(".BJ") else 0.0,
            })
            inst_rows.append({"date": pd.Timestamp(day), "instrument": code,
                              "name": base["name"], "type": "stock"})

    write_month(real_rows, root, "cn_stock_real_bar1d")
    write_month(adj_rows, root, "cn_stock_bar1d")
    write_month(pref_rows, root, "cn_stock_prefactors")
    write_year(inst_rows, root, "cn_stock_instruments")
    write_year(events_raw, root, "cn_stock_dividend")


if __name__ == "__main__":
    main()
