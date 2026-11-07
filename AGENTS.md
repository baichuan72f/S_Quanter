# AGENTS.md — 给维护这个仓库的智能体

这是 **SmallQuant**：BigQuant 模拟盘的本地离线复刻（Python 3.11 + pandas/numpy/duckdb + SQLite），
行情走本地 parquet（DuckDB 惰性读），撮合是自研引擎 `engine/smalltrader.py`，看板是本地 Web。

## 先跑通，再改代码

```bat
.venv\Scripts\python.exe queries.py                                  :: 数据视图自检，应全部 [✔]
.venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb --status
```

- 环境已在主 checkout 里修好（`.venv\pyvenv.cfg` 指向本机 Python 3.11.9）。
  如果 import pandas/duckdb 失败，跑一次 `修复环境.bat`。
- **当前没有自动化测试。** 任何改动至少要跑上面两条命令，并在 PR 里贴输出。

## 仓库边界（重要）

| 路径 | 是否入库 | 说明 |
|---|---|---|
| `engine/` `paper/` `strategies/` `datadownload/` `*.py` `*.ipynb` `*.bat` | ✅ | 代码与策略，改动主体 |
| `.venv/` | ❌ | 本地虚拟环境，206MB，不跨机器；已在 `.gitignore` |
| `data/` `*.parquet` | ❌ | 94MB 行情快照，二进制不可 diff；新增文件即生效，由 `datadownload/export_*.py` 在 BigQuant 上导出 |
| `state/*.db` `notifications/` `logs/` `__pycache__/` | ❌ | 运行时产物 |

**不要在 PR 里添加 `data/` 下的任何 parquet，也不要改 `.venv/`。**

执行器在工单目录里通过 junction 链接了主 checkout 的 `data/` 与 `.venv/`，
所以你在工单目录里直接跑上面的命令就能读到真实数据，但**它们不属于你的改动范围**。

## 约定

- `strategies/` 是**唯一权威策略目录**：跑批、看板「提交模拟」、编辑/删除都围绕它。
  研究阶段的 notebook 放仓库根目录，不参与跑批。
- 策略是 `.ipynb` + 同名 `.meta.json`（成对存在，改一个要同步另一个）。
- 注释、打印、错误信息用中文，与现有代码一致；Windows 路径优先（本项目只在 Windows 上跑）。
- 提交信息用 `type: 中文摘要`（如 `fix: 修正除权除息后复权价计算`），一次提交一件事。
- 不要引入新依赖；真要加，先在 PR 里说明并同步更新 `requirements.txt`（当前只有 pandas/numpy/duckdb 三个）。

## 拿到工单后

1. 先读 `README.md` 的相关小节和你要改的模块，再动手；
2. 改动前跑一次上面的自检命令，确认基线是好的；
3. 改完再跑一次，把输出贴进 PR；
4. PR 描述写清「改了什么、为什么、怎么验证的」。
