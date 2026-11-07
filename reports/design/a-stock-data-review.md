# a-stock-data 接入提案评审

> 评审对象：`reports/design/a-stock-data-integration.md`（PR #9，已合并到 `b5e5b3e`）
> 来源：GitHub issue #11
> 评审方式：只读核对代码与本地快照，未改代码、未改数据
> 评审结论：**方案 B 的方向成立，但不建议按现文档原样实施**；先修正列契约与对拍门禁，按本文的 B′ 执行。

---

## 1. 复核范围与结论

### 1.1 结论先说

1. 提案对现状的判断总体准确：10 张表的行数/日期范围、`_BAR_COLUMNS`、`connect(root=...)`、`LocalParquetDataSource(data_dir=...)`、README 的长窗口策略对照表，均与代码/数据一致。
2. 推荐的方案 B（DAI 默认 + 独立 vendor 目录 + 离线 ETL + 先对拍后切换）方向正确，**不应改成方案 A 直接替换**。
3. 但现文档有三处会直接卡住实施：
   - 把“缺列”当成引擎层可容忍，实际取数层会先抛 DuckDB Binder Error；
   - 首批三张表不足以跑现有 `PaperRunner`/`sim_replay`，名称/日历/分红表缺失会直接报错；
   - 用 `dual_ma_demo` 在现有 20 个交易日快照上做净值对拍，两边都不会产生有效成交，门禁是空的。
4. 因此建议的不是“不做 B”，而是 **B′：保持 DAI 默认、vendor 只读对拍；先补全列契约与对拍工具，再谈净值对拍和方案 C**。

### 1.2 不推荐的边界

- 不建议现在做方案 A：运行时联网、字段缺口、prefactors 不可复刻，提案这部分论证成立。
- 不建议现在做方案 C：没有可信的跨源对拍证据，也没有缺表显式失败的统一机制。
- 不建议把“B 阶段不改任何代码”写成硬承诺：若要用现有 runner/replay 读 vendor 数据根，至少需要一个显式数据根参数或独立对拍脚本。

---

## 2. 复核证据（本次实跑/核对）

以下均在工单环境用 `.venv/bin/python` 执行；Windows 下等价命令为 `.venv\Scripts\python.exe`。

### 2.1 基线可用

```text
$ .venv/bin/python queries.py
数据根目录: .../design-astock-review-5bcdd363420c/data
v_trading_days ... [✔]
v_cn_stock_real_bar1d ... [✔]
... 共 10 张表全部 [✔]
```

```text
$ .venv/bin/python run_daily.py --strategy strategies/小市值5日轮动2.ipynb --status
策略 小市值5日轮动2 尚未注册（从未跑过）
```

### 2.2 快照数字逐项核对

提案 §1.2 的 10 张表数字与实测一致，未发现偏差。关键值复现如下：

| 表 | 实测行数 | 日期范围 | 备注 |
|---|---:|---|---|
| `cn_stock_real_bar1d` | 117,094 | 2026-10-08 ~ 2026-11-05 | 5,582 个标的 |
| `cn_stock_bar1d` | 111,516 | 2026-10-08 ~ 2026-11-04 | |
| `cn_fund_real_bar1d` | 45,302 | 2026-10-08 ~ 2026-11-05 | |
| `cn_stock_index_bar1d` | 79,844 | 2024-01-02 ~ 2026-11-05 | 121 个指数 |
| `cn_stock_dividend` | 47,836 | 2010-01-15 ~ 2026-12-11 | 含未来除息日 |
| `cn_stock_instruments` | 1,428,829 | 2025-01-02 ~ 2026-11-04 | |
| `cn_stock_prefactors` | 112,638 | 2026-10-08 ~ 2026-11-04 | 151 列 |
| `trading_days` | 30,075 | 2010-01-04 ~ 2026-11-04 | 8 个 market_code，CN 4,087 天 |

补充核对：

- `cn_stock_real_bar1d` 的 `volume` 是股、`turn` 是小数、`adjust_factor` 是累计值（平安银行 2026-11-05：`adjust_factor=142.070212`、`turn=0.006333`），与提案一致。
- `trading_days` 的列名是 `market_code`（不是 `market`）；提案只写“8 个市场”，实现时若按 `market` 建 vendor 表会读不出来。
- 2026-10-08 ~ 2026-11-05 之间只有 **20 个 CN 交易日**；这是后文“净值对拍无效”的直接依据。

### 2.3 关键取数行为实跑

构造一个只含 8 列（无 `upper_limit/lower_limit/adjust_factor`）的临时 vendor 表，调用现有数据源：

```text
LocalDuckDataSource(data_root=...).get_bar_data(...)
→ BinderException: Binder Error: Referenced column "upper_limit" not found in FROM clause!
```

把 `upper_limit/lower_limit/adjust_factor` 建成 NULL 列后再调用：

```text
→ OK，返回列 = open/high/low/close/volume/pre_close/upper_limit/lower_limit/amount/adjust_factor
```

再用只有行情表的临时根调用 `LocalParquetDataSource.get_instrument_names`：

```text
→ CatalogException: Catalog Error: Table with name cn_stock_instruments does not exist!
```

以上三条是第 3 节 M1、M3 的实证。

---

## 3. 与代码/数据实际不符的地方

### M1（重要）“缺列留空”不会走到引擎，取数层先报错

提案 §1.1 说 `upper_limit/lower_limit/adjust_factor` 缺失时“引擎不会报错，会改变撮合与除权处理行为”，§7 第 2 步又写“缺列留空”。这句话只在**引擎拿到 DataFrame 之后**成立。

实际链路是：

- `engine/data_source.py:186-192` 用 `SELECT date, instrument, {', '.join(_BAR_COLUMNS)}` 显式取全部 10 列；
- `paper/local_data.py:158` 的 `get_bar_data` 直接委托给 `LocalDuckDataSource`；
- vendor 表若真的**不建**这些列，DuckDB 在查询阶段就抛 `BinderException`（见 §2.3），根本到不了引擎的 `if 'upper_limit' in bar_df.columns` 分支。

结论：ETL 必须为每张行情表**建齐 10 列**，不可用字段写 NULL 并在 manifest/报告里标注，不能省略列。否则方案 B 第 2 步的第一次读取就失败。

### M2（重要）分红契约列写少了，且不同层列名不同

提案 §1.1 写的分红契约是：

```text
ex_date/instrument/cash_before_tax/bonus_rate/conversed_rate/register_date
```

实际代码有三层：

| 层 | 实际列 |
|---|---|
| `data/cn_stock_dividend` 源表 | `date`、`ex_date`、`report_date`、`publish_date`、`bonus_rate`、`conversed_rate`、`cash_before_tax`、`cash_after_tax`、`register_date`、`instrument` |
| `engine/data_source.py:228-229` 查询 | `date`、`instrument`、`cash_before_tax`、`cash_after_tax`、`bonus_rate`、`conversed_rate`、`register_date`、`publish_date` |
| `paper/local_data.py:56-57` 输出 | `ex_date`、`instrument`、`cash_before_tax`、`bonus_rate`、`conversed_rate`、`register_date`（`date` 被重命名为 `ex_date`） |

因此：

- 按提案这份 6 列契约建 vendor 分红表，`get_dividend_events` 会因缺 `cash_after_tax`/`publish_date` 报 Binder Error；
- 上游 `dividend_history` 恰好没有 `cash_after_tax`/`publish_date`，ETL 必须补 NULL 列，或在适配层显式处理，不能假设“缺了也没事”；
- 对比时必须区分源表的 `date`、`ex_date` 和输出层的 `ex_date`，否则事件对齐会错一天。

### M3（重要）首批三张表不足以跑现有 runner/sim_replay

提案 §6.1 说首批对拍只做“股票日线、指数日线、交易日历”，名称/基金/分红“本期不承诺”；§7 第 4 步又计划用 `dual_ma_demo` 走现有链路做净值对拍。二者对不上。

现有 `PaperRunner.run_one_day` 在取行情之外还会调用：

- `paper/runner.py:644` `checks.check_data_ready(...)` → 需要 `trading_days`；
- `paper/runner.py:654` `get_index_bar_data(...)` → 需要 `cn_stock_index_bar1d`；
- `paper/runner.py:659` `get_dividend_events(...)` → 需要 `cn_stock_dividend`；
- `paper/runner.py:660` `get_instrument_names(...)` → 需要 `cn_stock_instruments`（基金还要 `cn_fund_instruments`）。

也就是说，要让现有 runner 直接指向 vendor 根，vendor 按股票/基金混合池至少要提供：股票行情、基金行情、指数、交易日历、股票分红、股票名称、基金名称（7 张表；策略若直连 `cn_fund_bar1d` 还要第 8 张）。只做三张表时，只能用**自写的 bar-level 对拍脚本**，不能直接跑 `PaperRunner`/`sim_replay`。

### M4（重要）用 `dual_ma_demo` 做跨源净值对拍，在当前快照上不会产生有效信号

- DAI 股票行情只有 2026-10-08 ~ 2026-11-05，共 20 个交易日；
- `dual_ma_demo` 的 `warmup_days=120`，`SLOW=20`，`handle_data` 中 `len(h) < 20` 直接 `continue`；
- 前 19 个交易日几乎不可能形成完整 MA20 信号，最后一天也难有“次日成交”形成净值差；
- `轮动宝2本地` 的 `warmup_days=120`，同样在 20 天快照上不产生有效成交。

结论：§7 第 4 步在当前数据上会得到两条近似水平的净值曲线，**“对拍通过”没有证据力**。要保留净值门禁，必须先扩 DAI 历史（或构造受控的对比数据），并断言成交笔数 > 0；否则应明确降级为“只做字段级对拍”。

### M5（中等）“B 阶段不动代码”说重了：现有 replay/runner 没有数据根入口

提案 §4 已写“可能需要给 `LocalParquetDataSource(data_dir=...)、sim_replay` 增加数据根参数”，但 §6.2 又说 `paper/runner.py`/`sim_replay.py` 在 B 阶段不动。实际代码：

- `sim_replay.py:262` 在对拍时硬编码 `LocalParquetDataSource()`；
- `run_daily.py:133,159` 直接 `LocalParquetDataSource()`；
- `bqcompat/dai_compat.py:62` 的 `_CONN` 是进程级单例，固定 `queries.connect()`；
- `engine/data_source.py:437-438` 的 `_auto_fetch_backtest_data` 也直接 `LocalDuckDataSource()`。

所以：

- `LocalParquetDataSource(data_dir=...)` 本身可用（提案这点是对的）；
- 但 `sim_replay`/`run_daily`/平台风格 `dai.query` 没有 `--data-dir` 或等价入口，不能“只改参数”切到 vendor 根；
- 方案 B 若不改主链路，应明确写“另写对拍脚本”，不要写成“现有 replay 可指定数据根”。

### M6（中等）`queries.connect` 缺表不是 `DaiTableError`

`queries.py:113-116` 只对存在 parquet 的表建视图；缺表时直接查询得到 DuckDB `CatalogException`。`DaiTableError` 只存在于 `bqcompat/dai_compat.py`，且 `available_tables()` 仍按默认 `DATA_ROOT` 判断。

因此：

- 方案 B 阶段，vendor 根缺表会表现为“表不存在”，不是统一的 `DaiTableError`；
- 方案 C 要求的“缺表显式抛 `DaiTableError`、绝不回退”不会自动成立，需要适配层/预检脚本；
- 也不能把“视图没建”当成“表为空”——两者的失败信息不同，验收时要区分。

### M7（较小）引用信息与依赖描述有陈旧/不精确

- 提案开头写 PR #7“未合并”，当前 master 已包含 `docs/数据源改造说明.md`（PR #7 已合并，合并时间 2026-11-07 10:06）；实施前应更新引用。
- §1.3 把 `ETF轮动_年化45_大宽` 与 `ML滚动_talib` 都写成“另依赖 sklearn/talib/lightgbm”；实际 `ETF轮动` 只 import `bigquant/math/numpy/pandas`，`sklearn/talib/lightgbm` 只在 `ML滚动_talib` 中出现。不影响结论，但影响工作量估计。
- `paper/local_data.py:51-53` 的 `_BAR_COLUMNS` 含 `name`，但 `get_bar_data` 实际不返回 `name`（引擎行情契约里没有它）；§7 第 2 步把 `name` 放进行情列对齐时，名称对拍应走 `cn_stock_instruments`，不要指望行情表。

---

## 4. 实施风险（按“会炸”程度排序）

“会炸”按两类排序：**能跑但结果错** 优先于 **直接报错**，因为前者更难发现。

### R1（最高）字段口径错，回测能跑但结果是错的

- **触发条件**：ETL 直接把 TDX/腾讯原始列映射成 canonical 列，未做单位/复权/事件口径换算。
- **具体会错的地方**：
  - `volume`：TDX 是股；腾讯是手（科创板例外），差 100 倍；
  - `amount`：腾讯 K 线没有，TDX 有；缺失后成交额类因子/校验全失效；
  - `adjust_factor`：DAI 是累计因子；TDX 没有；新浪是 qfq/hfq 比例因子，方向/口径不同；
  - `upper_limit/lower_limit`：两家都没有，缺失后一字板买不进/跌停卖不出的约束消失，成交偏乐观；
  - `turn`：DAI 是小数，baostock 是百分数，差 100 倍；
  - 分红：上游 `dividend_history` 的送/转是每 10 股，DAI 的 `bonus_rate/conversed_rate` 是每股，漏除 10 会把送转放大 10 倍；
  - 停牌：DAI 有行且 `volume=0`；TDX 盘后包可能没有无价记录，“缺行”会被 `checks.py` 判成数据残缺。
- **影响面**：选股因子、撮合约束、除权除息、净值曲线全部可能偏；且行情 schema 检查能通过，报告看起来正常，属于最危险的静默错误。
- **缓解**：先写字段映射表 + 单测；重叠窗口逐字段对拍；量纲不对直接退回，不进入净值对拍。

### R2（高）列/表契约不完整，第一次取数就报错

- **触发条件**：vendor 表省略 `adjust_factor`/`upper_limit`/`lower_limit`，或分红表缺 `cash_after_tax`/`publish_date`，或 vendor 根缺名称/日历/指数表。
- **影响面**：`get_bar_data`、`get_dividend_events`、`get_instrument_names` 分别抛 `BinderException`/`CatalogException`；第 2 步对拍直接中断；若脚本捕获异常后继续，会退化成“无数据/无成交”的假结果。
- **缓解**：ETL 输出**完整 canonical schema**（不可用列写 NULL），加 schema 校验和 manifest；缺表在预检阶段显式报错，不静默跳过。

### R3（高）跨源净值门禁无效，导致错误结论

- **触发条件**：按 §7 第 4 步，用现有 20 天快照跑 `dual_ma_demo`；或 `sim_replay` 两次都用了默认 DAI 根（因为它没有 vendor 根入口）。
- **影响面**：两条净值曲线都近似水平、成交为 0，却被当成“对拍通过”；后续据此推进方案 C，等于用无证据的结论做产品决策。
- **缓解**：先扩 DAI 历史或构造受控数据；对拍报告必须同时给出成交笔数、持仓变化；每次运行记录数据根和文件哈希，确认不是 DAI 对 DAI。

### R4（中高）数据根/连接缓存混用，结果不可复现

- **触发条件**：同一进程里先建了默认根的 `_CONN`/`_con_cache`，再传 vendor 根；或 `LOCALBQ_DATA_DIR` 只影响部分入口；或 `PaperRunner` 用 vendor、策略内部 `dai.query` 仍走 DAI。
- **影响面**：一次回测混用两套行情/分红/日历，净值不可复现；同一 `strategy_id` 切换来源会污染状态库曲线。
- **缓解**：一次运行只允许一个显式数据根；所有入口统一读取；报告记录根路径和清单哈希；禁止同一策略混源。

### R5（中）历史覆盖、停牌与发布时点造成缺口/误判

- **触发条件**：TDX 盘后包 404（非交易日/未发布/早于 2021-08-16）；2022-05-06 前无北交所文件；停牌日 TDX 不返回行；腾讯对北交所只返回 1 根并 `ValueError`。
- **影响面**：`checks.py` 对持仓缺行直接中止；候选池缺行超过 20% 也会中止；历史覆盖比 DAI 短；含北交所标的的策略在 early window 不可用。
- **缓解**：ETL 记录每个分区的来源 URL/下载时间/行数/校验和；缺口显式标注；不要用 0 或前值静默填充；先确定历史起点和北交所策略范围。

### R6（中低）上游不稳定、限流与数据许可

- **触发条件**：源站改版、腾讯单入口约 600 次后限流、东财 1 秒串行、上游没有包/SLA/版本化快照。
- **影响面**：ETL 维护成本高、补数失败、同一天重跑可能拿到不同数据；数据来自各源站，代码 Apache-2.0 不覆盖数据再分发。
- **缓解**：pin commit + vendor 副本 + manifest；只在本机自用、不提交数据；升级走单独 PR 并对拍。

---

## 5. 结论与建议（B′）

### 5.1 是否推荐方案 B

**推荐 B 的方向，但不推荐按现文档原样执行。** 理由：

- B 的核心优点仍然成立：运行期离线、DAI 默认、vendor 独立目录、最坏删目录即可回滚；
- A 的失败面（联网、字段缺口、prefactors 重写、平台对拍失效）没有变化，仍不应选；
- C 仍然必须建立在 B 的对拍证据上，现在没有证据，不应提前做。

### 5.2 建议改成什么：B′

1. **先定 schema 契约，再写 ETL。** 每张 vendor 表按 `queries.TABLES` + 引擎取数列建齐；不可用列写 NULL 并在 `manifest.json` 标注来源/单位/缺口。分红表同时保留 `date` 和 `ex_date`，并补齐 `cash_after_tax`/`publish_date`（可 NULL）。
2. **首批只做“表级对拍”，不跑 runner。** 用独立脚本通过 `queries.connect(root=...)` 读 vendor 根，比较主键、行数、覆盖率、OHLC 关系、量纲、名称/日历；这一步不需要名称/分红/指数全表，也不碰主流程。
3. **对拍工具与主链路解耦。** 需要跑 `dual_ma_demo` 时，用独立 `compare_runner.py` 显式传 `LocalParquetDataSource(data_dir=...)`；不要声称 `sim_replay` 已能选根。若确实要改 `sim_replay`/`run_daily`，只加只读 `--data-dir`，默认值不变，另开一个小 PR。
4. **净值门禁加前提。** 当前 DAI 只有 20 个交易日，先扩 DAI 历史到覆盖 warmup，或改做受控对比；对拍报告必须包含成交笔数 > 0、数据根哈希、最大偏差。做不到就明确把 B 定位为“校验用”，不进入方案 C。
5. **方案 C 暂不启动。** 不添加 `strategies/*.meta.json` 的 `data_source`，不看看板来源标签，不设计状态库混源；等 B′ 的对拍报告和缺表机制都齐了再单独提案。

### 5.3 如果只能“零代码改动”

则 B 的交付物应缩小为：

- `datadownload/astock/` 下的 ETL/校验脚本；
- `data_vendor/astock/` 的同 schema parquet（不入 Git）；
- 一份表级对拍报告（Markdown，不含大表）。

**不要**在这一版承诺“现有 runner 直接读 vendor”“跨源净值对拍已通过”；这两件事都需要额外的数据根入口和完整名称/日历/分红表。

---

## 6. 待确认

1. **B 是否允许最小代码改动？** 是给 `sim_replay`/`run_daily` 增加只读 `--data-dir`（默认不变），还是坚持只写独立对拍脚本、完全不动主链路？（默认：只写独立脚本。）
2. **首批 vendor 表范围？** 只做行情/指数/日历的 bar-level 对拍，还是一次补齐名称/分红/基金表以兼容现有 `PaperRunner`？（默认：先 bar-level，不承诺 runner 兼容。）
3. **净值对拍的门槛怎么定？** 先扩 DAI 历史到能覆盖 120 天 warmup 再做，还是接受“当前无法做净值对拍”、只保留字段级校验？（默认：先扩历史；做不到就取消净值门禁。）
4. **不可映射列怎么处理？** 是否接受 vendor 表建齐全部 canonical 列、不可用字段写 NULL 并逐项标注，而不是省略列或静默填 0？（默认：建齐 + NULL + 标注。）
5. **历史深度与北交所覆盖？** 接受 2021-08-16 起、2022-05-06 前无北交所，还是要求补更早历史/北交所后再验收？（默认：接受 2021-08 起，缺口显式标注。）
