"""SmallQuant 本地回测引擎包。

`engine` 作为正式包（非命名空间包）内聚引擎实现，与策略/数据解耦。
子模块：
- engine.smalltrader      — 回测引擎（API 对齐 bigtrader）
- engine.data_source      — 本地 DuckDB 数据源（import 即注册自动取数）
- engine.smalltrader_report — 绩效报告渲染

项目根目录通过 venv site-packages 的 smallquant_project_path.pth 注册到
sys.path，故策略可从任意 cwd 直接 `import engine.smalltrader` / `import queries`。
"""