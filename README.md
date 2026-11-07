# SmallQuant 本地模拟盘（独立分发包）

BigQuant 模拟盘的本地复刻：**全离线运行，不需要 BigQuant 账号、不需要安装 bigquant / bigtradercpp / dai**。
行情走本地 parquet（DuckDB 惰性读），状态走 SQLite，撮合是自写引擎（与平台逐笔对齐过）。

整个目录拷到任意 Windows 机器，装好 Python 3.11（或用内置 .venv）即可跑。

---

## 快速开始

1. 双击 **`修复环境.bat`**（首次运行；确认内置 .venv 可用，不可用会自动修复/重建）
   若第 2 步启动失败或报 `No Python at ...`，先双击 `修复环境.bat`：它会改写 `.venv\pyvenv.cfg` 指向本机 Python 3.11，必要时重建环境。
2. 双击 **`启动看板.bat`** → 浏览器打开 http://127.0.0.1:8765/
3. 命令行跑批：

```bat
.venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb --status
.venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb
```

---

## 目录结构

```
smallquant/
├─ .venv/                    内置 Python 3.11 环境（pandas / numpy / duckdb）
├─ data/                     本地 parquet 行情快照（DuckDB 建视图惰性读）
├─ engine/                   撮合引擎 smalltrader + 本地数据源（无 dai）
├─ paper/                    模拟盘：状态库 / 跑批 / 绩效 / 看板数据 / 提交后端
├─ state/paper.db            状态库（已带一份演示账户与净值历史，可删）
├─ strategies/               模拟运行中的策略（跑批 / 看板只认这里）
├─ notifications/            T+1 委托清单（跑批自动生成）
├─ dashboard.py              可视化看板（本地 Web）
├─ run_daily.py              跑批唯一入口
├─ queries.py                数据接口自检（打印数据根目录与各表可用性）
├─ sim_replay.py             历史回放（沙盒账户，不影响正式账户）
├─ 修复环境.bat / 启动看板.bat
└─ 小市值5日轮动.ipynb / 小市值3日轮动5只.ipynb / 轮动宝v3.ipynb / 小市值sample2平台数据.ipynb
                             研究版策略（根目录，仅作参考，不参与跑批）
```

约定：** `strategies/` 是唯一权威策略目录**，跑批、看板「提交模拟」、编辑/删除都围绕它；
研究阶段的 notebook 直接放根目录，不影响运行。

---

## 数据（先看这段）

数据是 DAI 日更导出的 parquet 快照，按目录分表，**新增文件即生效，不用重建视图**。

| 表 | 覆盖范围 | 说明 |
|---|---|---|
| `cn_stock_real_bar1d` | 2026-10、2026-11 | 股票未复权日线（回测主力表） |
| `cn_stock_bar1d` | 2026-10、2026-11 | 股票后复权日线 |
| `cn_stock_prefactors` | 2026-10、2026-11 | 预计算因子大表 |
| `cn_fund_bar1d` / `cn_fund_real_bar1d` | 2026-10-08 ~ 2026-11-05 | 场内基金日线（年度文件已按日期裁剪） |
| `cn_stock_index_bar1d` | 2010-2026 全年 | 指数日线（基准） |
| `cn_stock_dividend` | 2010-2026 全年 | 除权除息事件 |
| `trading_days` | 2010-2026 | 交易日历（`market_code='CN'`） |
| `cn_stock_instruments` / `cn_fund_instruments` | 2025 全年 + 2026-10 起 | 证券代码与简称 |

**所以：只有最近约一个月行情。** 依赖长回看窗口的策略跑批时不会报错，但因子算不出、**静默不下单**：

| 策略 | warmup_days | 一个月数据够不够 |
|---|---|---|
| 小市值5日轮动2 | 10 | 够，能出信号、能成交 |
| dual_ma_demo | 120 | 不够，不产生成交 |
| 轮动宝2本地 | 120 | 不够，不产生成交 |
| 小市值sample | 420 | 不够，不产生成交 |

想看长周期，把更多月份的 parquet 按同名目录补进 `data/` 即可（例如 `data/cn_stock_real_bar1d/202609.parquet`），无需改代码。
数据放到别处时用环境变量 `LOCALBQ_DATA_DIR` 指过去。

另一个小限制：交易日历只到 2026-11-04，而行情到 11-05，所以最后一天跑批会提示
「交易日历未覆盖 T+1，委托清单未生成」——不影响净值与持仓，补上日历后即正常。

自检数据是否就绪：

```bat
.venv\Scripts\python.exe queries.py
```

---

## 常用命令

```bat
:: 状态 / 净值
.venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb --status

:: 跑最新可得交易日（已跑过的日自动跳过）
.venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb

:: 指定日期 / 区间补跑
.venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb --date 2026-11-04
.venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb --from 2026-10-30 --to 2026-11-05

:: 从上次已跑日追到最新（漏跑自动补）
.venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb --catchup

:: 回滚某日（含）之后重跑 —— 不可撤销，慎用
.venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb --rollback-from 2026-11-04 --yes
```

看板参数：

```bat
.venv\Scripts\python.exe dashboard.py                       :: 默认 127.0.0.1:8765 并自动开浏览器
.venv\Scripts\python.exe dashboard.py --port 8899 --no-browser
.venv\Scripts\python.exe dashboard.py --host 0.0.0.0        :: 局域网可访问
```

---

## 写自己的策略

策略是「平台同形」的 `.py` 或 `.ipynb` 文件，放 `strategies/` 下即可被跑批与看板识别。
最小契约见 `strategies/dual_ma_demo.py`：

```python
STRATEGY = dict(
    strategy_id='demo',               # 状态库键；换 id = 全新账户
    strategy_name='示例',
    capital_base=1_000_000.0,
    instruments=['000001.SZA'],       # 候选池（平台风格代码）
    benchmark='000300.SH',
    order_price_field_buy='open',     # 计划单按 T+1 开盘价撮合
    order_price_field_sell='open',
    warmup_days=120,                  # 行情窗口前推的自然日
    initialize=initialize,
    handle_data=handle_data,
)
```

- `handle_data` 在**收盘后**运行，下的单次日开盘成交（日频，T+1 规则由引擎强制）
- 模拟盘每天冷启动、`initialize` 每天重跑 —— 跨日状态放 `context.user_store`，
  不要依赖模块级全局变量，也不要拿 `trading_day_index` 做调仓调度（单日引擎下恒为 0）
- 没有 `STRATEGY` 的文件由同目录 `<同名>.meta.json` 补参数（看板「提交模拟」会自动生成）

---

## 看板能做什么

- `/` 策略列表：总资产、累计收益、年化、回撤、夏普、持仓数、计划单数、最近运行时间；
  行内菜单可「立即运行 / 停止部署 / 编辑代码 / 删除」
- `/tasks` 任务管理：运行历史与实时日志
- 右上「提交模拟」：选一个 `.py`/`.ipynb` → 填参数 → 复制进 `strategies/` 并立即跑一次

---

## 环境

内置 `.venv` 是 Python 3.11.9，只装了运行必需的 **pandas 2.3.3 / numpy 1.26.4 / duckdb 1.5.5**。
`.venv` 里的 python 依赖本机 Python 3.11，换机器后：

1. 先双击 `修复环境.bat`（会用本机 Python 3.11 重写 `.venv\pyvenv.cfg`）
2. 若本机没有 Python 3.11，脚本会新建虚拟环境并 `pip install -r requirements.txt`（需联网）

手动重建：

```bat
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

可选依赖都没装：`matplotlib`（引擎的简易绩效图，缺失时自动跳过）、
`IPython`（notebook 内联展示）、`pyarrow`（DuckDB 读 parquet 自带，不需要）。
