# 接入 a-stock-data 作为新数据源的设计提案

> 状态：提案（未实施，不改代码）
> 日期：2026-11-07
> 来源：GitHub issue #8 `design: 接入 a-stock-data 作为新数据源的设计提案`
> 上游：`simonlin1212/a-stock-data`，V3.10.4，commit `f215349`（2026-11-04），Apache-2.0
> 相关：PR #7 中的 `docs/数据源改造说明.md`（未合并）已梳理数据层分层与通用改造步骤；
> 本文不重复通用步骤，只回答「a-stock-data 能补什么、不能补什么、怎么接」。

---

## 1. 现状

### 1.1 数据链路

```text
BigQuant DAI（云端，需账号）
  └─ datadownload/export_p0.py / export_dividend.py / export_prefactors.py
       （在 BigQuant 侧执行，依赖 bigquant + polars）
       └─ data/<表名>/<YYYYMM|YYYY>.parquet（不入 Git，约 94MB）
            ▼
        queries.py
          connect()：按 TABLES 建 v_<表> 与裸名视图，惰性 read_parquet
          DATA_ROOT：LOCALBQ_DATA_DIR → 项目内 data/ → 上一级 data/
            ├─ engine/data_source.py
            │    LocalDuckDataSource（StockDataSource 唯一实现）
            │    _auto_fetch_backtest_data 注册为 smalltrader._AUTO_LOADER
            ├─ paper/local_data.py
            │    LocalParquetDataSource：代码风格、标的池、交易日历、跑批辅助
            │    → paper/runner.py → paper/checks.py → smalltrader
            ├─ bqcompat/dai_compat.py
            │    把策略里的 dai.query / dai.DataSource 接到本地 DuckDB 视图
            └─ dashboard.py / paper/webdata.py / sim_replay.py
                 名称兜底、看板、历史回放
```

关键事实：

- 引擎只消费 `dict[symbol, DataFrame]`，不关心数据来源；换数据源的正式扩展点是
  `engine/data_source.py` 的 `StockDataSource` 抽象类与 `_auto_fetch_backtest_data`。
- `engine/data_source._BAR_COLUMNS` 实际裁剪行情列为：
  `open/high/low/close/volume/pre_close/upper_limit/lower_limit/amount/adjust_factor`。
  其中 `upper_limit`/`lower_limit`/`adjust_factor` 缺失时引擎不会报错，会改变撮合与
  除权处理行为（见 §6.4）。
- 分红事件契约：`ex_date/instrument/cash_before_tax/bonus_rate/conversed_rate/register_date`；
  基金没有独立分红表，`get_fund_dividend_events()` 用 `adjust_factor` 跳变反推份额折算。
- 策略还有两条绕过数据源抽象层的取数路径：
  1. 直接 `import queries; queries.connect().execute(...)`（小市值系列）；
  2. `import dai; dai.query(...)`（`bqcompat` 转发到同一个 DuckDB）。
  因此 `queries` 模块名、`connect()`、`TABLES`、`DATA_ROOT` 本身也是对外契约。

### 1.2 当前快照（2026-11-07 在工单环境实测）

| 表 | 行数 | 日期范围 | 说明 |
|---|---:|---|---|
| `trading_days` | 30,075 | 2010-01-04 ~ 2026-11-04 | 8 个市场；`CN` 4,087 天 |
| `cn_stock_real_bar1d` | 117,094 | 2026-10-08 ~ 2026-11-05 | 未复权日线，回测主力表 |
| `cn_stock_bar1d` | 111,516 | 2026-10-08 ~ 2026-11-04 | 后复权日线 |
| `cn_fund_real_bar1d` | 45,302 | 2026-10-08 ~ 2026-11-05 | 场内基金未复权日线 |
| `cn_fund_bar1d` | 43,140 | 2026-10-08 ~ 2026-11-04 | 场内基金后复权日线 |
| `cn_stock_index_bar1d` | 79,844 | 2024-01-02 ~ 2026-11-05 | 指数日线，121 个指数 |
| `cn_stock_instruments` | 1,428,829 | 2025-01-02 ~ 2026-11-04 | 股票代码/简称快照 |
| `cn_stock_dividend` | 47,836 | 2010-01-15 ~ 2026-12-11 | 含已公告的未来除息日 |
| `cn_fund_instruments` | 3,264,948 | 2010-01-04 ~ 2026-11-04 | 基金代码/简称快照 |
| `cn_stock_prefactors` | 112,638 | 2026-10-08 ~ 2026-11-04 | 151 列预计算因子大表 |

实测口径细节：

- 股票/基金行情 `volume` 单位是**股/份**（例：平安银行 2026-11-05
  `volume=122,899,701`、`amount=1.429e9`，与「量 × 均价」吻合）；`turn` 是**小数**
  （0.006333 = 0.63%）。
- 股票行情 `adjust_factor` 是累计复权因子（平安银行 2026-11-05 为 142.070212），
  不是「当日/昨日的比值」。
- 指数表没有 `adjust_factor`/`upper_limit`/`lower_limit`，另有 `change`/`change_ratio`/`turn`。
- 分红表 `bonus_rate`/`conversed_rate` 是**每股**送/转比例，`cash_before_tax` 是每股税前现金。

### 1.3 既有策略怎么取数

| 策略 | 取数方式 | 依赖表 |
|---|---|---|
| `strategies/小市值5日轮动2.ipynb` | `queries.connect()` 后直接 SQL | `cn_stock_prefactors`（`st_status`、`is_risk_warning`、`total_market_cap`、`volume`、`close`） |
| `strategies/小市值sample.ipynb` | 同上 | `cn_stock_prefactors` |
| `strategies/轮动宝2本地.ipynb` | `queries.connect()` 后直接 SQL | `cn_fund_bar1d.close` |
| `strategies/dual_ma_demo.py` | `data.history(...)`，由 `_auto_fetch_backtest_data` 自动取数 | `cn_stock_real_bar1d` + 指数 |
| `strategies/ETF轮动_年化45_大宽.ipynb`、`ML滚动_talib.ipynb` | `from bigquant import bigtrader, dai`（平台写法） | `bqcompat` + 平台表；另依赖 sklearn/talib/lightgbm，基础 `.venv` 未安装 |

### 1.4 当前痛点

1. **本地只有约一个月股票/基金行情。** `warmup_days` 大于覆盖范围的策略不报错，
   因子算不出后**静默不下单**（README 已列出对照表：`dual_ma_demo` 120 天、
   `轮动宝2本地` 120 天、`小市值sample` 420 天在当前快照下都不产生成交）。
2. **补数据依赖 BigQuant 账号与平台侧脚本。** `datadownload/export_*.py` 需要
   `bigquant`/`polars`/`log_util`，不是本地脚本；没有平台账号的人无法自行补历史。
3. **单一来源，缺独立校验。** 行情、分红、因子同出 DAI，导出错位或字段口径变化时，
   本地没有第二来源可以对拍。
4. **数据更新是手工/半手工。** 当前没有新鲜度监控、清单和自动校验（PR #7 已列为 P0/P1 缺口）。

---

## 2. 问题

### 2.1 要解决什么

issue #8 的目标是：把 a-stock-data 作为新数据源接进 SmallQuant，**替代或补充**现有
DAI 本地快照，并先比较三种做法（直接替换、并行双源、按策略可切换）。

拆开看是三个不同层次的需求，混在一起会导致方案失真：

1. **数据可获得性**：能否不依赖 BigQuant 账号、拿到更长历史的 A 股日线/指数/分红/估值？
2. **数据可信度**：能否用第二来源交叉校验现有 DAI 快照？
3. **能力扩展**：能否给策略新增资金流、龙虎榜、分钟线、财务、公告等 DAI 没有的数据？

a-stock-data 对第 1、2 项只能**部分**满足，对第 3 项很强；它不是一套可直接替换的
历史行情库，也不是可安装的 Python 包。

### 2.2 a-stock-data 是什么（按上游仓库实测）

- 仓库主体是一个 **`SKILL.md`**（7,702 行，91 个 `python` 代码块），把 34 个公开数据源的
  抓取代码内嵌在 Markdown 里，供 AI 编程助手直接抄用；**没有** `pyproject.toml`/
  `setup.py`/`requirements.txt`，不能 `pip install`，也没有 HTTP 服务或本地数据库。
- 宣称 15 层、87 个端点、34 个来源、除 iwencai 外零鉴权；提供行情/K 线、复权因子、
  全市场当日日线、研报、资金流、龙虎榜、新闻、财务、公告、打板、期权、宏观、
  指数与交易日历、期货、事件驱动、可转债等。
- 依赖：`mootdx requests pandas stockstats numpy baostock xlrd openpyxl`（README 快速开始）。
  与本仓库「运行依赖只有 pandas/numpy/duckdb」冲突。
- 数据来自公开网站/交易所接口，**没有 SLA、没有版本化数据快照**；上游会随源站改版
  修函数（CHANGELOG 从 V3.x 到 V3.10.4），代码与数据口径都可能漂移。
- 与行情回测直接相关的能力：
  - `tdx_daily_package(date)`：通达信官网每日盘后包，一次拿沪深北全市场某交易日日线，
    含 `prev_close/open/high/low/close/volume/amount/name`；约 2.7MB/天；文档给出的
    实测样本最早为 2021-08-16（2022-01-04、2023-01-03 可取，2021-01-04 已 404），
    并注明「未逐日验证」。
  - `tencent_kline(code, period, adjust, start, end)`：按标的取日/周/月 K 线，支持
    qfq/hfq/不复权；单段最多 640 根，按 700 天分段翻页；**不含成交额**；腾讯对北交所
    只返回最新 1 根，函数直接 `ValueError`；单域名约 600 次后限流，三入口轮换。
  - `sina_adjust_factor(code, kind)` + `apply_adjust(bars, factors)`：新浪 qfq/hfq 比例因子。
  - `dividend_history(code)`：东财分红送转历史，返回
    `date/bonus_rmb(每股派息)/transfer_ratio(每10股转增)/bonus_ratio(每10股送股)`。
  - `trading_calendar(year, month)`：深交所整月日历，逐月请求，未发布月份抛错。
  - `baostock_valuation_history(code, s, e)`：2016 起日频 PE/PB/PS/PCF、换手率、
    停牌状态、`isST`；**不支持北交所**，逐标的登录 baostock 查询。
  - `baostock_stock_basic(code)`：上市日/退市日/状态，逐标的查询。
- 与回测无直接关系、但可作为后续能力扩展的：分钟线/逐笔、北向、资金流、龙虎榜、
  解禁、两融、大宗、股东户数、概念板块、新闻、公告、涨停池、期权、可转债、宏观等。

### 2.3 不解决的后果

- 长回看窗口策略继续「能跑但不交易」，问题只能靠人看报告发现。
- 数据错误仍然只有一个来源，无法定位是 DAI 导出问题还是策略逻辑问题。
- 若未来需要分钟线、资金面、财务等策略，仍要另起一套取数工程。

---

## 3. 约束

| 约束 | 具体内容 | 来源 |
|---|---|---|
| 离线分发 | 项目要能整目录拷到 Windows 机器离线运行；运行期不能依赖公网接口 | README「全离线运行」 |
| 依赖最小化 | 运行依赖只有 pandas/numpy/duckdb；新增依赖要先说明并同步 `requirements.txt` | `requirements.txt`、AGENTS.md |
| 表契约 | 表名、列名、代码风格（`.SZ/.SH`）、分区粒度、`v_<表>`/裸名视图不能随意改 | `queries.py`、`engine/data_source.py` |
| 引擎字段 | `pre_close`、`adjust_factor`、`upper_limit`/`lower_limit` 参与撮合/除权；缺失不报错但会改变结果 | `engine/smalltrader.py` |
| 策略直连 | 小市值系列直接 SQL `cn_stock_prefactors`，换源不能只换行情表 | 策略 notebook |
| 平台一致性 | 当前策略与 BigQuant 平台同形，回测结果依赖 DAI 口径；换源会破坏逐笔对拍前提 | README、`sim_replay --verify` |
| 数据不入库 | `data/`、`*.parquet` 被 `.gitignore` 排除，PR 不能带数据 | `.gitignore` |
| 无自动化测试 | 改动至少要跑 `queries.py` 与 `run_daily.py --status`，PR 贴输出 | AGENTS.md |
| 上游许可 | 代码 Apache-2.0；数据来自各网站/交易所，**代码许可不覆盖数据本身**，行情/分红数据的使用与再分发需遵守各源 ToS | 上游 LICENSE/README |
| 上游不稳定 | 无包版本、无数据快照、无 SLA；接口随源站改版而变化 | 上游 CHANGELOG |

另外两个必须提前说清的限制：

1. **a-stock-data 不能复刻 `cn_stock_prefactors` 的 151 列。** 其中
   `total_market_cap`/`float_market_cap`/`total_shares`/`a_shareholder`/财务同比/
   概念集合/两融状态/涨跌停状态/申万行业历史等，来自 BigQuant 的加工数据；
   a-stock-data 只有部分原始字段（估值、ST、行业当前值等），无法保证同口径。
2. **a-stock-data 不是历史数据库。** 历史日线要靠「逐日下载盘后包」或「逐标的翻页」
   自行落盘；覆盖深度、下载耗时、限流和缺口都要自己承担。

---

## 4. 方案对比

### 方案 0（基线）：不改，继续用 DAI 快照 + 手工补历史

- 做法：维持现状；需要长历史时在 BigQuant 上跑 `export_p0.py` 补月份。
- 改动范围：0。
- 优点：零风险；与平台口径完全一致；不新增依赖。
- 缺点：仍依赖 BigQuant 账号；无法独立校验；长窗口策略仍不可用。
- 迁移成本：0。

### 方案 A：直接替换 —— 运行时从 a-stock-data 取数

- 做法：新增 `AStockDataSource(StockDataSource)`，在
  `get_bar_data`/`get_dividend_events` 内直接调用上游函数（腾讯 K 线、TDX 盘后包、
  东财分红等），让 `run_daily`/回测在运行时联网取数。
- 改动范围：`engine/data_source.py` 新增实现并改默认 loader；`paper/local_data.py`
  增加对应适配；`queries.py` 需要把 `connect()` 变成「按需拉取」或另建表；
  `paper/checks.py`、`sim_replay.py` 的表名/日历假设都要改。
- 优点：看起来最彻底，不再需要 DAI 导出。
- 缺点/风险：
  - 直接破坏「离线可跑、结果可复现」；同一天两次运行可能拿到不同数据（源站回填/改版）。
  - 字段缺口硬伤：TDX 盘后包没有 `adjust_factor`/`upper_limit`/`lower_limit`/
    `turn`/`deal_number`；腾讯 K 线没有 `amount`/`pre_close`；两者都不能直接满足
    `_BAR_COLUMNS` 与引擎除权逻辑。
  - 需要 `requests`/`baostock` 等新依赖，且 Windows 离线分发包无法保证。
  - 全市场逐标的取数会触发限流（腾讯单域名约 600 次、东财 1 次/秒串行），
    每日跑批时间不可控；北交所腾讯不支持。
  - `cn_stock_prefactors` 无法复刻，小市值策略必须重写选股逻辑（产品决策，不是数据改造）。
  - 策略与 BigQuant 平台的回测对拍前提消失，历史结果不可比。
- 迁移成本：高；且失败面覆盖数据、策略、分发三条线。

### 方案 B：并行双源 —— DAI 保持默认，a-stock-data 落成独立数据集做补充/校验

- 做法：写一个**离线 ETL**（只在更新数据时联网），把 a-stock-data 的能力落成
  **与现有表同 schema 的 parquet**，放在独立根目录（建议 `data_vendor/astock/`，
  不入 Git），目录名沿用 `cn_stock_real_bar1d/` 等；运行时仍走 DAI 的 `data/`。
  用 `queries.connect(root=data_vendor/astock)` 即可读出同一套视图，不改 `queries.py`。
  先用重叠窗口（2026-10-08 ~ 2026-11-05）做字段级对拍，再决定是否让它参与跑批。
- 改动范围：
  - 新增 ETL 目录（建议 `datadownload/astock/` 或 `vendor/astock/`）与映射/校验脚本；
  - 可能需要给 `LocalParquetDataSource(data_dir=...)`、`sim_replay` 增加数据根参数；
  - `queries.py` 可保持不动（`connect(root=...)` 已支持）。
- 优点：
  - 不破坏离线运行与现有策略；DAI 仍是唯一默认口径。
  - 有第二来源可做字段级、净值级对拍，能真正回答「DAI 是否可信」。
  - 可以先只做能完整映射的表（股票日线、指数、交易日历），逐步扩。
  - 数据落地后可复现，不受源站当天状态影响。
- 缺点/风险：
  - 需要维护 ETL、映射、调度、校验，工作量集中在数据工程而非策略。
  - TDX 盘后包文档实测样本最早为 2021-08-16（且「未逐日验证」），更早历史要逐标的
    翻腾讯 K 线，成本高；北交所只能用 TDX，腾讯不支持。
  - 字段缺口仍在（复权因子、涨跌停价、成交笔数、换手率、市值、prefactors），
    只能「部分表可替代」，不能宣称完整替换。
  - 依赖上游代码版本；建议 pin commit + vendor 副本，不直接 import `SKILL.md`。
- 迁移成本：中；可分阶段，先只读对拍，不影响线上跑批。

### 方案 C：按策略可切换 —— 数据源成为策略级配置

- 做法：在方案 B 的数据集基础上，给策略元信息/CLI 增加 `data_source`
  （`bigquant` 默认 / `astock`），`paper/runner.py`、`sim_replay.py` 与
  `_auto_fetch_backtest_data` 按配置选择数据根；看板显示来源标签。
- 改动范围：`strategies/*.meta.json`、`paper/runner.py`、`run_daily.py`、
  `sim_replay.py`、`engine/data_source.py`、`queries.py`（可能需要数据根注册）、
  `dashboard.py`/`paper/webdata.py`（展示）、文档。
- 优点：同一策略可在两套数据上对比，逐步迁移；不强迫所有策略一起换。
- 缺点/风险：
  - 策略行为变成「取决于配置」，回测/模拟结果的可比性和审计成本上升；
    状态库没有数据源字段，同一 `strategy_id` 切换来源会造成净值曲线混源。
  - 小市值系列仍依赖 `cn_stock_prefactors`，切到 `astock` 时该表不存在，
    必须显式失败（`DaiTableError`），不能静默空表。
  - 需要额外的 UI、文档、回归矩阵；在字段口径未验证前上线会放大错误。
- 迁移成本：高；依赖方案 B 的数据与对拍结论。

### 对比速览

| 维度 | 方案 0 不改 | A 直接替换 | B 并行双源 | C 按策略切换 |
|---|---|---|---|---|
| 离线可跑 | ✅ | ❌ | ✅ | ✅（两套本地数据） |
| 结果可复现 | ✅ | ❌ | ✅ | ✅ |
| 摆脱 BigQuant 账号 | ❌ | ⚠️ 部分表可以 | ⚠️ 部分表可以 | ⚠️ 部分表可以 |
| 保留平台口径 | ✅ | ❌ | ✅（默认仍是 DAI） | ✅（默认仍是 DAI） |
| 可交叉校验 DAI | ❌ | ❌ | ✅ | ✅ |
| 长历史行情 | 取决于 DAI 补数 | ⚠️ 2021-08 起较现实 | ⚠️ 同左 | ⚠️ 同左 |
| prefactors 兼容 | ✅ | ❌ | ✅（不碰） | ⚠️ 需显式禁用 |
| 新增依赖 | 无 | 多个 | ETL 侧可选，运行侧无 | 同 B |
| 改动/风险 | 无 | 高 | 中 | 高 |

---

## 5. 推荐方案

**推荐方案 B：并行双源，先做「可对拍的补充数据集」，不替换默认数据源。**

理由：

1. **a-stock-data 的能力边界决定了它不能做「直接替换」。** 它是抓取配方集合，不是
   历史行情库；日线字段缺 `adjust_factor`/涨跌停价/成交笔数，且 prefactors 无法复刻。
   直接替换会把「数据源改造」变成「策略重写 + 引擎字段补齐 + 分发方式改变」三件事。
2. **SmallQuant 的核心价值是离线和可复现。** 方案 B 把联网限制在 ETL 更新阶段，
   运行时仍是本地 parquet，保持现有分发包与跑批链路不变。
3. **先有对拍证据，再谈替换。** 重叠窗口的字段级差异、分红事件差异、净值差异，
   是判断「能否替换」的唯一依据；没有这些数据，方案 C 的切换开关只是把不确定性
   交给使用者。
4. **成本可控、可回退。** 数据放在独立目录，最坏情况删掉即可；默认路径不变。

推荐方案的边界（明确放弃的东西）：

- **不承诺替代 DAI。** `cn_stock_prefactors`、基金份额折算、涨跌停价等仍以 DAI 为准；
  a-stock-data 只覆盖能对上的表。
- **不做实时/分钟级。** 本期只做日频 EOD；分钟线/逐笔是后续独立能力，不混进日线链路。
- **不改策略代码。** 小市值系列继续读 DAI prefactors；在方案 B 阶段不提供
  `astock` 运行开关。
- **历史深度以「能稳定拿到」为准。** 优先文档实测覆盖的 2021-08-16 之后的 TDX 盘后包；
  更早历史是否补，等 §9 的确认项。

如果后续确认要「部分策略改用 a-stock-data」，再实施方案 C；C 必须建立在 B 的对拍
报告和显式失败机制之上，默认仍为 DAI。

---

## 6. 影响面

### 6.1 表级可映射性

| SmallQuant 表 | a-stock-data 可用能力 | 可替代性 | 主要缺口 |
|---|---|---|---|
| `cn_stock_real_bar1d` | `tdx_daily_package(date)`（全市场单日）；`tencent_kline(adjust='')`（逐标的） | ⚠️ 部分 | TDX 无 `adjust_factor`/涨跌停价/`turn`/`deal_number`；腾讯无 `amount`/`pre_close`；成交量单位不同（TDX=股，腾讯=手，科创板例外） |
| `cn_stock_bar1d` | 不复权价 + `sina_adjust_factor(hfq)`/`apply_adjust` | ⚠️ 需换算 | 复权口径与 DAI 的累计 `adjust_factor` 是否等价**待对拍**；基金复权 V3.10.4 才修 |
| `cn_fund_real_bar1d` / `cn_fund_bar1d` | `tencent_kline` 支持 ETF/LOF；`sina_adjust_factor` 基金 hfq | ⚠️ 部分 | 缺基金全量清单、`iopv`、涨跌停价、`turn`；ETF 是否包含在 TDX 盘后包**待确认** |
| `cn_stock_index_bar1d` | `tencent_kline` 指数（需 `sh`/`sz` 前缀） | ⚠️ 部分 | 无 `amount`/`turn`/涨跌停；指数清单要自己维护；`change_ratio` 需计算 |
| `cn_stock_dividend` | `dividend_history(code)`（东财，逐标的） | ⚠️ 部分 | 只有 `date/bonus_rmb/transfer_ratio(每10股)/bonus_ratio(每10股)/plan`；缺 `cash_after_tax`/`register_date`/`publish_date`/`report_date`；需 ÷10 换算；逐标的请求受限流 |
| `trading_days` | `trading_calendar(year, month)`（深交所，逐月） | ⚠️ 部分 | 只能按月请求；历史月份是否可查**待确认**；无其他市场；无 `market_code` |
| `cn_stock_instruments` | `baostock_stock_basic`（逐标的上市/退市/状态）+ 腾讯当日快照 | ❌ 不足 | 没有批量历史快照；逐标的 5,000+ 次请求；无每日 `date` 快照语义 |
| `cn_fund_instruments` | 未见等价全量清单 | ❌ 不足 | 基金代码/简称历史快照缺失 |
| `cn_stock_prefactors` | 可重算部分技术指标；`baostock_valuation_history` 提供 PE/PB/PS/PCF/换手/停牌/`isST` | ❌ 不可替代 | 缺 `total_shares`/`total_market_cap`/`float_market_cap`/股东户数/财务同比/概念/两融/申万历史/涨跌停状态等；151 列无法同口径复刻 |

结论：**能进入首批对拍的是股票日线、指数日线、交易日历；分红可作第二优先级；
基金、名称快照、prefactors 不建议在本期承诺。**

### 6.2 模块与函数

| 位置 | 影响 |
|---|---|
| `queries.py` | 方案 B 可不动；只需用 `connect(root=...)` 读 `data_vendor/astock/`。若增加数据源注册/清单，才需要改 `TABLES` 或 `DATA_ROOT` |
| `engine/data_source.py` | B 阶段不动。若做 C，需要新增 provider 选择、`_BAR_COLUMNS` 兼容（缺列）与显式缺表报错 |
| `paper/local_data.py` | B 阶段可用 `LocalParquetDataSource(data_dir=...)` 做对拍；C 阶段增加数据源参数，`expand_pool`/`latest_bar_date`/`_read_parts` 跟随 |
| `paper/checks.py` | 硬编码 `cn_stock_real_bar1d`/`cn_fund_real_bar1d` 与基金前缀；只要目录结构同 schema，无需改；若日历/基金分类变化则要同步 |
| `bqcompat/dai_compat.py` | `_SUPPORTED` 只影响缺表提示；方案 B 不需要新增表；C 阶段切到 vendor 根时须保持「缺表抛 `DaiTableError`」 |
| `paper/runner.py` | B 阶段不动；C 阶段要读策略的 `data_source` 并把数据根传给 `LocalParquetDataSource` |
| `dashboard.py` / `paper/webdata.py` | B 阶段不动；C 阶段要显示数据来源，避免同一 `strategy_id` 混源 |
| `sim_replay.py` | B 阶段需要能指定数据根做对拍；C 阶段要记录回放使用的数据源 |
| `run_daily.py` | C 阶段增加 `--data-source`；B 阶段不动 |
| `strategies/*.meta.json` | C 阶段新增 `data_source` 字段；B 阶段不动 |

### 6.3 既有策略脚本

- `小市值5日轮动2` / `小市值sample`：**最大阻塞项**。选股 SQL 直接读
  `cn_stock_prefactors` 的 `st_status`、`is_risk_warning`、`total_market_cap`；
  a-stock-data 没有等价表，切换到 vendor 数据会直接 `Catalog Error`/`DaiTableError`。
  方案 B 不触碰这两个策略；若未来要迁移，必须先确定「总市值/ST 历史」的替代口径
  并重写 SQL，属于产品决策。
- `轮动宝2本地`：只读 `cn_fund_bar1d.close`，理论上可由「腾讯 K 线 + 新浪 hfq」重建，
  但需验证复权口径与基金清单；B 阶段只对拍，不切换。
- `dual_ma_demo`：只用 `data.history`，是最适合做首批跨源净值对拍的策略
  （不依赖 prefactors）。
- `ETF轮动_年化45_大宽`、`ML滚动_talib`：依赖平台包和额外科学计算依赖，当前基础
  `.venv` 就跑不通，不在本期范围。

### 6.4 回测结果与字段口径

只要数据参与撮合，就必须把下列口径差异当成「结果可能变化」处理，而不是实现细节：

| 字段/口径 | DAI 现状 | a-stock-data | 影响 |
|---|---|---|---|
| `volume` 单位 | 股/份 | TDX=股；腾讯=手（科创板例外为股） | 成交量类因子、成交额核对、流动性过滤全部偏差 100 倍 |
| `amount` | 元 | TDX=元；腾讯 K 线**无** | 依赖成交额的策略/校验缺失 |
| `pre_close` | 有 | TDX 有 `prev_close`；腾讯 K 线无 | 涨跌停、除权参考价、当日收益计算 |
| `adjust_factor` | 有（累计值，如 142.07） | TDX 无；新浪为 qfq/hfq 比例因子 | 股票送转回退、基金份额折算、复权价全部受影响 |
| `upper_limit`/`lower_limit` | 有 | TDX/腾讯均无（腾讯实时快照只有当日） | 一字板买不进/跌停卖不出的约束消失，成交结果偏乐观 |
| `turn` | 小数（0.0063=0.63%） | baostock `turn` 是百分数（0.31=0.31%） | 换手率因子差 100 倍 |
| 分红比例 | 每股 `bonus_rate` | 每 10 股 `bonus_ratio`/`transfer_ratio` | 忘记 ÷10 会把送转放大 10 倍 |
| 分红现金 | `cash_before_tax`/`cash_after_tax` | `bonus_rmb` 每股税前；无税后 | 红利税/成本调整口径 |
| 分红日期 | `date`=除权除息日，另有 `register_date`/`publish_date` | `date`=除息日；无登记日/公告日 | 事件对齐、T+1 处理 |
| 停牌 | 有行、`volume=0` | TDX 盘后包无价记录可能不出现；baostock `tradestatus` | 「缺行」与「停牌」语义不同，`checks.py` 会把缺行判为数据残缺 |
| ST/风险警示 | `st_status`/`is_risk_warning` | baostock `isST`（不支持北交所）；`is_risk_warning` 无等价 | 小市值选股范围变化 |
| 复权方式 | 后复权表 + 累计因子 | 新浪比例因子 / 腾讯等差前复权 | 长区间价格与收益率不可直接比 |

**对回测结果的影响判断**：在完成重叠窗口对拍之前，不能声称「替换后结果不变」。
即便价格差异很小，涨跌停价、停牌语义、分红比例任一差异都可能改变成交与净值；
`sim_replay --verify` 的 1e-4 阈值只适用于「同一份数据」的逐日/一次性对拍，
跨数据源必须另定阈值并报告最大偏差。

### 6.5 额外能力（不是替代）

a-stock-data 还能提供 DAI 本地快照没有的数据：分钟线/逐笔、北向、资金流、龙虎榜、
两融、大宗、股东户数、概念板块、新闻、公告、涨停池、期权、可转债、宏观利率等。
这些需要**新增表/新策略契约**，属于独立需求；本提案不把它们塞进现有 10 张表，
避免「数据源接入」变成无边界的平台重写。

---

## 7. 分步实施计划

每一步都独立可验证；前一步不通过不进入下一步。

### 第 0 步：记录基线（不改代码）

- 做什么：跑现有自检与回放，记录表清单、行数、日期范围、策略状态与净值基线；
  保存到工单/PR 描述（不要提交数据文件）。
- 验证：
  ```bat
  .venv\Scripts\python.exe queries.py
  .venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb --status
  .venv\Scripts\python.exe sim_replay.py --strategies strategies\dual_ma_demo.py ^
      --start-date 2026-10-08 --max-days 20 --interval 0 --verify
  ```
- 产出：基线数字（本文 §1.2 已记录 2026-11-07 的快照，可作为对照）。

### 第 1 步：固定上游副本与 ETL 骨架

- 做什么：
  - 新建 `datadownload/astock/`（或 `vendor/astock/`），放入：
    `README.md`（上游 URL、V3.10.4、commit `f215349`、Apache-2.0、改动说明）、
    `fetch_tdx_daily.py`（按交易日下载并解析盘后包）、`mapping.py`（TDX → canonical 列）、
    `manifest.json`（每个分区文件的来源 URL、下载时间、行数、校验和）。
  - 只复制需要的上游函数（TDX 盘后包解析、腾讯 K 线、新浪复权因子），
    **不**在运行时 import `SKILL.md`；pin commit，升级走单独 PR。
  - ETL 依赖（如 `requests`）只放 ETL 侧的可选依赖文件，**不加入**运行 `requirements.txt`；
    能改用标准库 `urllib`/`zipfile` 的部分优先标准库。
- 验证：对 2026-11-05 下载一次盘后包，解析出全市场行数；与 DAI 当日行数对比，
  记录差异；`manifest.json` 可复现同一次下载的校验和。
- 注意：TDX 当日包通常收盘后数小时才发布；非交易日返回 404，必须显式报错，不能写空表。

### 第 2 步：首批三张表的映射与对拍（只读）

- 做什么：
  - 用 TDX 盘后包重建重叠窗口 `2026-10-08 ~ 2026-11-05` 的
    `cn_stock_real_bar1d`（先不补历史），列对齐 `date/instrument/name/pre_close/open/
    high/low/close/volume/amount`，缺列留空；
  - 用腾讯 K 线重建 `cn_stock_index_bar1d` 的 `000300.SH`（基准）作抽样对拍；
  - 用深交所日历重建 `trading_days`，与 DAI 的 `CN` 日历对比。
  - 输出写到 `data_vendor/astock/<表名>/`，目录布局与 `queries.TABLES` 一致；
    用 `queries.connect(root='data_vendor/astock')` 直接读，不污染 `data/`。
- 验证：
  - 主键（`date+instrument`）唯一、OHLC 关系成立、行数/标的数覆盖；
  - 与 DAI 同日同标的按代码 join，输出 `close/open/high/low/pre_close/volume/amount/name`
    的差异分布与样例（脚本可放 `datadownload/astock/compare.py`）；
  - 差异报告写到 `reports/`（文本/Markdown，不含大表）。
- 判定：先看覆盖率与量纲，再看价格；量纲不对（volume 手/股、turn 百分数/小数）
  直接退回修正映射，不进入净值对拍。

### 第 3 步：分红、估值/ST 与可替代性验证

- 做什么：
  - 用 `dividend_history` 抽样 20~50 只股票，与 `cn_stock_dividend` 对比
    `date`、每股现金、送/转比例（÷10 后）；
  - 用 `baostock_valuation_history` 抽样对比 `isST`、停牌、换手率；
  - 明确记录「prefactors 不可替代」的字段清单，避免后续误用。
- 验证：抽样差异表 + 结论（哪些字段可直接用、哪些需换算、哪些不可用）；
  对不可用字段在映射层显式抛错或置空并标注，不静默填 0。

### 第 4 步：跨源净值对拍

- 做什么：用 `dual_ma_demo.py`（不依赖 prefactors）在 DAI 与 vendor 日线上各跑
  同一区间，比较每日净值、成交笔数、持仓；再用 `轮动宝2本地` 对拍基金行情
  （若基金映射通过）。
- 验证：
  - 输出逐日净值差、最大回撤差、成交笔数差；
  - 阈值建议：日收益差绝对值 ≤ 0.1%、累计净值差 ≤ 0.5%、成交笔数差 = 0；
    阈值需人确认（见 §9），未达标就只保留「校验用」定位，不进入方案 C。
- 注意：小市值系列不参与跨源净值对拍（prefactors 无法替代），只作为
  「不改动」的回归项。

### 第 5 步（可选，需人批准）：按策略切换

只有第 2~4 步对拍通过且确认要迁移时才做：

- 做什么：
  - `strategies/*.meta.json` 增加 `data_source`（默认 `bigquant`）；
  - `paper/runner.py`、`run_daily.py`、`sim_replay.py` 读取该字段并选数据根；
  - `engine/data_source.py` 的自动取数按环境变量/显式注册选根，缺表时抛
    `DaiTableError`，绝不回退到另一来源；
  - 看板显示来源；状态库增加来源记录或明确禁止同一 `strategy_id` 混源。
- 验证：
  - 默认（不写字段）跑批结果与基线完全一致；
  - 显式 `astock` 的策略能跑通，缺 prefactors 时明确报错；
  - 看板来源标签正确；回放记录来源。

### 第 6 步：文档与清理

- 更新 `README.md` 数据章节、`docs/数据源改造说明.md`（PR #7 合并后）、
  `requirements*.txt`（仅 ETL 可选依赖）、`.gitignore`（`data_vendor/` 不入库）；
- 在 PR 中贴 `queries.py`、`run_daily --status`、对拍报告摘要与回滚步骤。

---

## 8. 验收判据与回滚

### 8.1 验收判据

数据层（方案 B）：

- `queries.py` 对现有 10 张表全部 `[✔]`，行数/日期范围与基线一致（默认 `data/` 未被修改）。
- `queries.connect(root='data_vendor/astock')` 能建出首批表视图；主键唯一、OHLC 关系
  成立、无 NaN 价格、`date` 为日期类型。
- 对拍报告包含：覆盖率、价格差异分布、成交量/额量纲核对、名称差异、日历差异；
  所有差异有归类（源差异/映射 bug/缺失字段），没有「未解释」项。
- 运行依赖未增加：`requirements.txt` 仍是 pandas/numpy/duckdb；`data_vendor/` 被忽略。

行为层（若进入方案 C）：

- 默认路径回归：`dual_ma_demo` 与 `小市值5日轮动2` 的 `--status`、跑批结果与基线一致；
  `sim_replay --verify` 通过（同一数据源内 1e-4）。
- 跨源对拍：`dual_ma_demo` 日收益差/累计净值差/成交笔数达到 §7 第 4 步阈值（待确认）。
- 缺表行为：`astock` 策略访问 `cn_stock_prefactors` 时抛 `DaiTableError`，不返回空表、
  不静默不下单。
- 离线验证：断网后仅用本地 parquet 跑通 `queries.py` 与一次跑批。

### 8.2 回滚

- 方案 B：删除/移走 `data_vendor/astock/`，默认 `data/` 未变，无需代码回滚；
  对拍脚本随分支回滚即可。
- 方案 C：
  1. 把策略 `meta.json` 的 `data_source` 改回 `bigquant`（或删除字段）；
  2. 回滚 `paper/runner.py`/`run_daily.py`/`sim_replay.py`/`engine/data_source.py`
     的选择逻辑；
  3. 重启看板/跑批进程（`queries.connect()` 的视图文件列表在连接时固定）；
  4. 不迁移、不删除 `state/paper.db`；若已用 `astock` 跑过，按 `strategy_id` 回滚
     对应绩效/状态（现有 `--rollback-from` 只覆盖日期，不覆盖数据源切换，需人工确认）。
- 上游代码：vendor 副本按 commit 固定；升级失败时回退到上一个 commit 副本，
  不修改 `data/`。

---

## 9. 待确认

每条都需要人拍板；括号内是默认建议。

1. **目标是「替代」还是「补充」？** 若是替代，必须接受策略重写与平台对拍失效；
   若是补充，按方案 B 推进。（默认：补充。）
2. **更新数据时允许联网吗？在哪个环境跑 ETL？** Windows 本机 / 另一台机器 /
   BigQuant 侧？运行期是否要求完全离线？（默认：允许更新时联网，运行期离线。）
3. **依赖策略**：接受「vendor 复制上游函数 + ETL 侧可选依赖」，还是要求
   `pip install a-stock-data`（上游没有包，需要先改造上游）？（默认：vendor 复制。）
4. **历史深度要到哪一年？** TDX 盘后包现实起点约 2021-08；更早要逐标的翻腾讯 K 线，
   成本和缺口都更大。（默认：先做 2021-08 起，重叠窗口优先。）
5. **是否要求与 BigQuant 平台逐笔/净值完全一致？** 若是，a-stock-data 只能做校验，
   不能参与跑批；若允许「独立可用但数值略有差异」，才考虑方案 C。
   （默认：先要求对拍达标，不承诺完全一致。）
6. **prefactors 怎么办？** 接受小市值系列继续只用 DAI，还是投入重写选股逻辑
   （总市值/ST 历史替代口径）？（默认：继续只用 DAI。）
7. **数据再分发与 ToS**：a-stock-data 代码是 Apache-2.0，但行情/分红数据来自各网站，
   是否只在本机自用、不随分发包分发？（默认：只本机自用，不提交、不再分发。）
8. **切换粒度**：只按策略切换，还是也要按表切换（例如行情用 vendor、因子用 DAI）？
   按表切换会显著增加混源与审计复杂度。（默认：只按策略，默认 bigquant。）
9. **失败策略**：vendor 数据缺失/过期时，是中止跑批还是回退 DAI？
   （默认：中止并显式报错，不在一次跑批中混用两个来源。）

---

## 附：证据与来源

- 仓库现状：`queries.py`、`engine/data_source.py`、`paper/local_data.py`、
  `paper/checks.py`、`paper/runner.py`、`strategies/*.ipynb`、`datadownload/*.py`；
  表行数/范围由 2026-11-07 工单环境 `queries.connect()` 实测。
- 上游：`https://github.com/simonlin1212/a-stock-data`，V3.10.4，
  commit `f21534946c42be3ad048ac938eecb1bad84cb8ab`；README/SKILL.md 中的
  端点说明、限流、字段单位、历史范围均按该版本记录。
- 相关文档：PR #7 `docs/数据源改造说明.md`（未合并）提供数据层分层、表契约、
  验收命令与已知坑；本文引用其结论，不重复展开。
