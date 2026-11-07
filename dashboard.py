#!/usr/bin/env python3
"""
dashboard.py —— 模拟盘可视化看板（本地 Web 服务）
=================================================

参照 BigQuant「我的交易」列表页的形态，把本地模拟盘状态可视化：

    /                     策略列表：收益/回撤/持仓/待执行计划单
    /s/<strategy_id>      策略详情：KPI 卡 + 净值与基准曲线 + 回撤
                          + 当前持仓 + 明日交易计划 + 历史成交 + 委托 + 日志
    /tasks                任务管理：左栏任务列表（状态筛选 + 搜索）
    /tasks/<strategy_id>  任务详情：状态/元信息 + 运行历史 + 实时日志
    /ide                  可选：内嵌 code-server / JupyterLab（需 --ide-url）

「提交模拟」两步弹窗：选文件（/api/browse）→ 填参数（/api/probe 预填）
→ 落盘到 strategies/ 并立即跑一次（/api/submit）。

用法
----
    python dashboard.py                          # 默认 state/paper.db，端口 8765
    python dashboard.py --db state/paper.db --port 8765
    python dashboard.py --no-browser             # 不自动开浏览器
    python dashboard.py --host 0.0.0.0           # 局域网可访问（默认只绑 127.0.0.1）
    python dashboard.py --browse-root D:/Data/bq # 提交时的文件浏览根目录
    python dashboard.py --ide-url http://127.0.0.1:8080/?folder=D:/Data/bq

设计约定
--------
- **写操作**：跑批（/api/run）、提交任务（/api/submit），以及列表页「操作」
  列 ⋯ 菜单里的停止（/api/stop）、删除（/api/delete）。跑批都走 `run_daily.py`
  同一套 PaperRunner 流程（含数据完整性校验），不做任何绕过校验的降级跑批；
  提交任务额外把策略文件复制进 `strategies/` 并写 meta.json。
- **删除是双份的**：状态库账户（`reset_strategy`）+ `strategies/` 下的源文件
  与 `<同名>.meta.json`，不可恢复 —— 前端有明确确认框。
- **单任务串行**：同一时刻只允许一个跑批任务，避免两个进程同时写状态库。
- **零新依赖**：HTTP 用标准库 http.server；图表用 ECharts CDN（离线时图表不显示，
  表格数据照常可读，页面会给出提示）。
- **颜色**：图表配色取自 dataviz 参考调色板已校验的相邻槽位（策略=蓝、基准=橙、
  回撤=红），浅色/深色两套各自选步，不是自动反色。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from contextlib import redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit

_ROOT = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_ROOT / 'engine')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from paper import submit, webdata  # noqa: E402
from paper.runner import PaperRunner, RunStopped, StrategySpec  # noqa: E402
from paper.store import StateStore  # noqa: E402

DEFAULT_DB = str(_ROOT / 'state' / 'paper.db')
DEFAULT_PORT = 8765
ECHARTS_CDN = 'https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js'


# ══════════════════════════════════════════════════════════════════
# 格式化工具
# ══════════════════════════════════════════════════════════════════
def h(v: Any) -> str:
    return html.escape('' if v is None else str(v))


def f_money(v, dash: str = '—') -> str:
    return f'{float(v):,.2f}' if v is not None else dash


def f_pct(v, signed: bool = True, dash: str = '—') -> str:
    if v is None:
        return dash
    return f'{float(v):+.2%}' if signed else f'{float(v):.2%}'


def f_num(v, n: int = 2, dash: str = '—') -> str:
    return f'{float(v):,.{n}f}' if v is not None else dash


def f_int(v) -> str:
    return f'{int(v):,}' if v is not None else '—'


def delta(v, fmt=f_pct) -> str:
    """带方向箭头的数值（方向不靠颜色单独承载）。"""
    if v is None:
        return '—'
    arrow = '▲' if float(v) > 0 else ('▼' if float(v) < 0 else '·')
    return f'{arrow} {fmt(v)}'


def tone_of(v) -> str:
    """涨跌配色（A 股习惯：正=红 .up，负=绿 .down）。"""
    if v is None:
        return ''
    return 'up' if float(v) > 0 else ('down' if float(v) < 0 else '')


# ══════════════════════════════════════════════════════════════════
# 页面样式（配色见模块 docstring；浅/深两套各自选步）
# ══════════════════════════════════════════════════════════════════
CSS = """
*,*::before,*::after{box-sizing:border-box}
/* 配色对齐 BigQuant 网页：A 股习惯（涨/正=红、跌/负=绿），品牌红 #e8474b，
   基准蓝、相对收益橙。浅/深两套各自选步（深色不是简单反色）。 */
:root{
  color-scheme:light;
  /* 取值自平台线上 CSS + 官方页面截图取色：
     正文 #202328、次要 #757a88、弱化 #9396AB（表头/标签灰）、
     涨红 .color-up=#e74c3c、跌绿 .color-down=#24bb85、警示 .color-warn=#ff8300、
     主按钮 #ec3825、分隔线 #eff0f2、常规边框 #e9eaec、页面底 #f5f5f5。
     ⚠ 跌绿曾误用 #00a870、警示曾误用 #faad14、弱化灰曾误用 #9195a0，
     三者都与平台不一致，已按平台实测值改回。 */
  --page:#f5f5f5; --surface:#ffffff; --ink:#202328; --ink2:#757a88; --muted:#9396ab;
  --sub:rgba(117,122,136,.5);
  --line:#eff0f2; --border:#e9eaec; --head-bg:#fafafa; --hover:#fafafa;
  --brand:#e74c3c; --btn:#ec3825; --up:#e74c3c; --down:#24bb85;
  --brand-bg:#fdf2f1;
  --strat:#e74c3c; --bench:#4a90d9; --excess:#f5a623;
  --grid:#eff0f2; --axis:#eff0f2;
  /* 板块标签：对齐平台列表页策略名后的「股票」蓝色小标 */
  --tag:#135bcf; --tag-bg:rgba(19,91,207,.05);
  /* 任务状态（对齐平台徽标：已完成=绿、运行中=蓝、失败=红） */
  --ok:#24bb85; --run:#1890ff; --warn:#ff8300;
  --overlay:rgba(0,0,0,.45);
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){
  color-scheme:dark;
  --page:#141414; --surface:#1f1f1f; --ink:#e8e8e8; --ink2:#bfbfbf; --muted:#8c8c8c;
  --sub:rgba(191,191,191,.55);
  --line:#303030; --border:#303030; --head-bg:#262626; --hover:#262626;
  --brand:#ef6b5e; --btn:#f05a48; --up:#ef6b5e; --down:#2fbf85;
  --brand-bg:rgba(239,107,94,.16);
  --strat:#ef6b5e; --bench:#5b9bd5; --excess:#faad14;
  --grid:#262626; --axis:#303030;
  --tag:#7fb0e8; --tag-bg:rgba(91,155,213,.16);
  --ok:#2fbf85; --run:#4c9aff; --warn:#ffa940;
  --overlay:rgba(0,0,0,.6);
}}
:root[data-theme="dark"]{
  color-scheme:dark;
  --page:#141414; --surface:#1f1f1f; --ink:#e8e8e8; --ink2:#bfbfbf; --muted:#8c8c8c;
  --sub:rgba(191,191,191,.55);
  --line:#303030; --border:#303030; --head-bg:#262626; --hover:#262626;
  --brand:#ef6b5e; --btn:#f05a48; --up:#ef6b5e; --down:#2fbf85;
  --brand-bg:rgba(239,107,94,.16);
  --strat:#ef6b5e; --bench:#5b9bd5; --excess:#faad14;
  --grid:#262626; --axis:#303030;
  --tag:#7fb0e8; --tag-bg:rgba(91,155,213,.16);
  --ok:#2fbf85; --run:#4c9aff; --warn:#ffa940;
  --overlay:rgba(0,0,0,.6);
}
html,body{margin:0;padding:0}
body{
  background:var(--page); color:var(--ink);
  /* 字族顺序对齐平台（-apple-system/Segoe UI/Roboto/Noto Sans），
     末尾保留中文回退，Windows 下与平台同为微软雅黑渲染 */
  font:14px/1.5715 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
       "Helvetica Neue",Arial,"Noto Sans","PingFang SC","Microsoft YaHei",
       "Hiragino Sans GB",sans-serif;
  -webkit-font-smoothing:antialiased;
}
a{color:inherit;text-decoration:none}
a:hover{color:var(--brand)}
/* 内容宽对齐平台列表页实测：卡片 1330px + 左右各 24px 内边距 = 1378px */
.wrap{max-width:1378px;margin:0 auto;padding:24px 24px 72px}
/* ── 页头 ── */
header.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px}
header.top h1{font-size:20px;margin:0;font-weight:600}
.crumb{color:var(--muted);font-size:13px}
.spacer{flex:1}
.tag{
  display:inline-block;padding:1px 8px;border-radius:2px;font-size:12px;line-height:18px;
  border:1px solid var(--border);color:var(--ink2);background:var(--surface);
}
/* 运行中：品牌红描边（本地原有的 A 股红语义，保持不动） */
.tag.ok{color:var(--up);border-color:var(--up)}
/* 最近一次的运行结果：对齐平台 —— 成功是绿勾、失败是红 */
.tag.done{color:var(--ok);border-color:var(--ok)}
.tag.fail{color:var(--up);border-color:var(--up)}
.tag.warn{color:var(--muted)}
/* 板块标签：对齐平台列表页策略名后的「股票」蓝色小标（浅蓝底、蓝字、无描边） */
.tag.stock{
  color:var(--tag);border:none;background:var(--tag-bg);
  border-radius:3px;padding:4px 6px;line-height:12px;
}
/* 已停止：中性灰实心，与「运行中」的红、「已完成/失败」的默认框区分开
   （注意本主题 --up 是红、--down 是绿，中国行情配色，勿按欧美语义取色） */
.tag.stopped{color:var(--muted);border-color:var(--muted);background:var(--head-bg)}
.dot{color:var(--muted);margin:0 4px}
.statusline{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px;margin:10px 0 26px}
.statusline .run{display:inline-flex;align-items:center;gap:5px;background:var(--surface);
  border:1px solid var(--border);border-radius:14px;padding:2px 12px;color:var(--ink2)}
.btn{
  appearance:none;border:1px solid var(--border);background:var(--surface);color:var(--ink);
  padding:6px 14px;border-radius:2px;font-size:14px;cursor:pointer;font-family:inherit;
  line-height:1.5715;
}
.btn:hover{color:var(--brand);border-color:var(--brand)}
.btn.primary{background:var(--btn);border-color:var(--btn);color:#fff}
.btn.primary:hover{filter:brightness(1.06);color:#fff}
.btn.dl{background:var(--btn);border-color:var(--btn);color:#fff}
.btn.dl:hover{filter:brightness(1.06);color:#fff}
.btn[disabled]{opacity:.45;cursor:default}
.btn[disabled]:hover{color:var(--ink);border-color:var(--border)}
/* ── 指标行（无卡片框，大号数值 + 下方灰标签）── */
.metrics{
  display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px;
  margin:0 0 6px;padding:8px 0 2px;
}
@media (max-width:1100px){.metrics{grid-template-columns:repeat(4,minmax(0,1fr));row-gap:22px}}
@media (max-width:640px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
.metric .v{font-size:30px;font-weight:600;line-height:1.25;letter-spacing:-.01em}
.metric .k{color:var(--muted);font-size:13px;margin-top:8px}
.metrics-sub{
  display:flex;flex-wrap:wrap;gap:22px;color:var(--muted);font-size:13px;
  padding:14px 0 26px;border-bottom:1px solid var(--line);margin-bottom:30px;
}
.metrics-sub b{color:var(--ink2);font-weight:500}
/* ── 区块 ── */
/* 对齐平台：内容一律落在白底圆角卡片上（页面底 #f5f5f5 + 白卡） */
.section{background:var(--surface);border-radius:10px;padding:24px;margin-bottom:16px}
/* ── 顶部总览（复刻平台「模拟交易总览」）───────────────────────────
   平台登录态实测（卡片 data-v-d408fbf9）：
     卡片    白底 / 圆角 **3px** / padding 20px 24px
     左列    标题 18px·600·#202328（行高 22px、上留 3px）与
             日期 14px/20px·rgba(117,122,136,.5) **上下堆叠**，间距 8px
     右侧    指标行 flex gap:70px —— 与标题**同一行**，不是标题下方另起一行
     指标    标签 14px/20px·#202328 + 数值 22px/22px，两者间隔 10px
   ⚠ 两处极易看反：① 指标行在标题右侧（看截图容易以为在下方）；
     ② 总览卡圆角 3px，而下面表格卡是 10px，两者**不一样**，别统一。 */
.section.summary{border-radius:3px;padding:20px 24px}
.summary-head{display:flex;align-items:flex-start;justify-content:space-between;
  gap:24px;flex-wrap:wrap}
.summary-id{display:flex;flex-direction:column;gap:8px}
.summary-title{font-size:18px;font-weight:600;line-height:22px;color:var(--ink);
  margin-top:3px}
.summary-date{font-size:14px;line-height:20px;color:var(--sub)}
.summary-metrics{display:flex;flex-wrap:wrap;gap:70px}
.sm{display:flex;align-items:center;gap:10px}
.sm .k{font-size:14px;line-height:20px;color:var(--ink)}
.sm .v{font-size:22px;line-height:22px;font-variant-numeric:tabular-nums}
/* 顶部计数条：对齐平台（15px·正文色，条目间隔 44px，图标与文字间隔 4px） */
.counters{display:flex;flex-wrap:wrap;gap:44px;justify-content:flex-end;
  margin:0 0 12px;font-size:15px;line-height:20px;color:var(--ink)}
@media (max-width:900px){.summary-metrics{gap:28px}.counters{gap:20px}}
.section-head{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px}
.section-head h2{font-size:16px;font-weight:600;margin:0}
.section-head .grow{flex:1}
.section-head .ctl{display:flex;align-items:center;gap:8px}
/* ── 表格 ── */
/* 表体 14px/22px、单元格四周 16px（行高 ≈57px）；
   表头白底、文字 12px #9396AB、下边框 #eff0f2：均按平台列表页实测对齐 */
table{width:100%;border-collapse:collapse;font-size:14px;line-height:22px}
thead th{
  background:transparent;color:var(--muted);font-weight:400;font-size:12px;
  padding:16px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap;
}
tbody td{
  padding:16px;text-align:right;border-bottom:1px solid var(--line);
  white-space:nowrap;font-variant-numeric:tabular-nums;
}
th:first-child,td:first-child{text-align:left}
th.l,td.l{text-align:left}
tbody tr:hover td{background:var(--hover)}
.tbl-wrap{overflow:auto}
.inst{display:flex;flex-direction:column;gap:2px;line-height:1.35}
.inst .nm{color:var(--ink)}
.inst .nm .tag{margin-left:6px;vertical-align:1px}
.inst .cd{color:var(--muted);font-size:12px}
.empty{color:var(--muted);padding:22px 4px;font-size:13px;text-align:center}
/* ── 分页（对齐平台：‹ 1 2 3 … N › 10条/页 跳至 __ 页）── */
.pager{display:flex;align-items:center;justify-content:center;gap:8px;
       padding:20px 0 2px;font-size:13px;color:var(--ink2);flex-wrap:wrap}
.pager button{
  min-width:32px;height:32px;padding:0 6px;border:1px solid var(--border);background:var(--surface);
  border-radius:2px;cursor:pointer;color:var(--ink2);font-family:inherit;font-size:13px;
}
.pager button:hover:not([disabled]){color:var(--brand);border-color:var(--brand)}
.pager button[aria-current="true"]{border-color:var(--brand);color:var(--brand)}
.pager button[disabled]{opacity:.4;cursor:default}
.pager .dots{color:var(--muted);padding:0 2px}
.pager select,.pager input{
  height:32px;border:1px solid var(--border);border-radius:2px;background:var(--surface);
  color:var(--ink2);padding:0 8px;font-family:inherit;font-size:13px;
}
.pager input{width:56px;text-align:center}
/* ── 图表 ── */
.chart{width:100%;height:380px}
.chart.dd{height:200px}
.chart-fallback{
  display:none;color:var(--muted);font-size:13px;padding:34px 8px;text-align:center;
  border:1px dashed var(--border);border-radius:2px;
}
/* ── 缩放时间 chips + 区间指标 ── */
.rangebar{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-bottom:6px}
.chips{display:flex;gap:0}
.chip{
  border:1px solid var(--border);border-right:none;background:var(--surface);color:var(--ink2);
  padding:4px 16px;font-size:13px;cursor:pointer;font-family:inherit;line-height:1.5715;
}
.chip:first-child{border-radius:2px 0 0 2px}
.chip:last-child{border-right:1px solid var(--border);border-radius:0 2px 2px 0}
.chip[aria-pressed="true"]{background:var(--brand);border-color:var(--brand);color:#fff}
.chip[aria-pressed="true"]+.chip{border-left-color:var(--brand)}
.range-stat{color:var(--muted);font-size:13px}
.range-stat b{font-weight:600;margin-left:4px}
.legend-hint{display:flex;gap:16px;color:var(--ink2);font-size:13px;align-items:center}
.legend-hint i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
details.tv{margin-top:14px}
details.tv>summary{cursor:pointer;color:var(--muted);font-size:13px;padding:4px 0}
/* ── 跑批面板 ── */
.runbox{margin-bottom:22px}
.runform{
  display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-top:12px;
  padding:14px 16px;border:1px solid var(--border);background:var(--surface);font-size:13px;
}
.runform label{display:flex;gap:6px;align-items:center;color:var(--ink2)}
.runform input[type=date]{
  border:1px solid var(--border);background:var(--surface);color:var(--ink);
  border-radius:2px;padding:4px 8px;font-family:inherit;font-size:13px;
}
.runlog{
  margin:0;padding:14px 16px;background:var(--surface);border:1px solid var(--border);
  border-top:none;font:12px/1.7 ui-monospace,Consolas,monospace;color:var(--ink2);
  max-height:300px;overflow:auto;white-space:pre-wrap;
}
.msg{padding:12px 16px;border:1px solid var(--border);background:var(--surface);
     color:var(--ink2);font-size:13px;margin-bottom:18px}
.msg.bad{color:var(--brand);border-color:var(--brand)}
.up{color:var(--up)} .down{color:var(--down)} .muted{color:var(--muted)}
.log-day{margin-bottom:12px}
.log-day .d{color:var(--muted);font-size:12px;margin-bottom:4px}
.log-line{font:12px/1.7 ui-monospace,Consolas,monospace;color:var(--ink2)}
.log-line .lv{color:var(--brand)}

/* ══ 顶部导航（我的交易 / 任务管理 / 提交模拟 / 编辑器） ══ */
.nav{display:flex;align-items:center;gap:6px;margin-bottom:18px;
     border-bottom:1px solid var(--border);padding-bottom:10px;flex-wrap:wrap}
.nav a.nav-a{padding:6px 14px;border-radius:6px;color:var(--ink2);font-size:14px}
.nav a.nav-a:hover{background:var(--hover);color:var(--ink2)}
/* 当前页签：对齐平台「策略 / 账户」分段控件 —— 淡红底 + 品牌红字（截图取色 #fdf2f1） */
.nav a.nav-a[aria-current="page"]{background:var(--brand-bg);color:var(--brand);
     font-weight:600}
.nav .spacer{flex:1}

/* ══ 任务管理器：左列表 + 右详情（对齐平台版式） ══ */
.tasks{display:grid;grid-template-columns:352px minmax(0,1fr);gap:18px;
       align-items:start}
@media (max-width:960px){.tasks{grid-template-columns:1fr}}
.tasks-left{background:var(--surface);border:1px solid var(--border);
     border-radius:8px;padding:12px;max-height:calc(100vh - 150px);overflow:auto}
.tasks-search{display:flex;gap:8px;margin-bottom:10px}
.tasks-search input{flex:1;padding:7px 10px;border:1px solid var(--border);
     border-radius:6px;background:var(--surface);color:var(--ink);font-size:13px}
.tabs{display:flex;gap:4px;margin-bottom:12px;flex-wrap:wrap}
.tabs button{border:1px solid transparent;background:transparent;color:var(--ink2);
     padding:4px 10px;border-radius:14px;font-size:13px;cursor:pointer}
.tabs button[aria-pressed="true"]{background:var(--head-bg);color:var(--ink);
     border-color:var(--border);font-weight:600}
.tabs .cnt{color:var(--muted);margin-left:3px}
.task{display:block;position:relative;border:1px solid var(--border);
     border-radius:8px;padding:12px 30px 12px 14px;margin-bottom:10px;
     background:var(--surface);box-shadow:0 1px 2px rgba(0,0,0,.03)}
.task:hover{border-color:var(--brand);color:inherit}
.task[aria-current="true"]{border-color:var(--brand);
     box-shadow:0 2px 10px rgba(0,0,0,.08)}
.task .row1{display:flex;align-items:center;gap:6px;font-size:12px;margin-bottom:6px}
.task .dot{width:7px;height:7px;border-radius:50%;background:var(--muted);flex:none}
.task .dot.ok{background:var(--ok)} .task .dot.error{background:var(--up)}
.task .dot.running{background:var(--run)}
.task .st{color:var(--ink2)}
.task .st.ok{color:var(--ok)} .task .st.error{color:var(--up)}
.task .st.running{color:var(--run)}
.task .kind{color:var(--muted)}
.task .name{font-size:15px;font-weight:600;color:var(--ink);margin-bottom:8px;
     word-break:break-all}
.task .meta{display:flex;align-items:center;gap:12px;font-size:12px;
     color:var(--muted);flex-wrap:wrap}
.task .chev{position:absolute;right:12px;top:50%;transform:translateY(-50%);
     color:var(--muted);font-size:15px}
.tasks-right{min-width:0}
.task-head{display:flex;align-items:flex-start;gap:12px;margin-bottom:14px;
     flex-wrap:wrap}
.task-head h1{margin:0;font-size:26px;font-weight:700;word-break:break-all}
.task-head .spacer{flex:1}
.chips{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12px;
     color:var(--ink2);margin-bottom:20px}
.chip{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;
     border:1px solid var(--border);border-radius:4px;background:var(--surface);
     font-size:12px;color:var(--ink2);max-width:340px;overflow:hidden;
     text-overflow:ellipsis;white-space:nowrap}
.chip.plain{border:none;background:transparent;padding:0}
.chip .dot{width:7px;height:7px;border-radius:50%;background:var(--muted)}
.chip .dot.ok{background:var(--ok)} .chip .dot.error{background:var(--up)}
.chip .dot.running{background:var(--run)}
.chip a{color:var(--run)}
.logview{margin:0;padding:14px 16px;background:var(--surface);
     border:1px solid var(--border);border-radius:8px;
     font:12px/1.7 ui-monospace,Consolas,monospace;color:var(--ink2);
     max-height:420px;overflow:auto;white-space:pre-wrap}
.logview:empty::after{content:'暂无日志';color:var(--muted)}
.hist-tabs{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.hist-tabs .on{padding:4px 12px;border:1px solid var(--border);border-radius:6px;
     background:var(--head-bg);font-size:13px;font-weight:600}

/* ══ 弹窗（提交任务两步流程） ══ */
.modal[hidden]{display:none}
.modal{position:fixed;inset:0;background:var(--overlay);display:flex;
     align-items:center;justify-content:center;z-index:50;padding:24px}
.dialog{background:var(--surface);border:1px solid var(--border);
     border-radius:10px;width:min(720px,100%);max-height:calc(100vh - 48px);
     display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.28)}
.dialog.wide{width:min(900px,100%)}
.dialog-head{flex:none;display:flex;align-items:center;gap:10px;padding:16px 20px;
     border-bottom:1px solid var(--border);font-size:16px;font-weight:600}
.dialog-head .spacer{flex:1}
/* min-height:0 是关键：否则 flex 子项按内容撑高，把底部按钮顶出视口 */
.dialog-body{flex:1 1 auto;min-height:0;padding:18px 20px;overflow:auto}
.dialog-foot{flex:none;display:flex;justify-content:flex-end;gap:10px;
     padding:14px 20px;border-top:1px solid var(--border)}
.close-x{border:none;background:transparent;color:var(--muted);font-size:18px;
     cursor:pointer;line-height:1;padding:0 4px}
/* ── 列表页「操作」列的 ⋯ 菜单（对齐平台：点开是一个竖排菜单）──
   position:fixed + JS 定位：表格外层 .tbl-wrap 是 overflow:auto，
   绝对定位的下拉会被裁掉，所以用 fixed 并挂在 body 上。 */
.menu-wrap{display:inline-block}
.menu-btn{border:none;background:transparent;color:var(--muted);cursor:pointer;
     font-size:18px;line-height:1;padding:2px 9px;border-radius:2px;letter-spacing:1px}
.menu-btn:hover{color:var(--brand);background:var(--head-bg)}
.menu{position:fixed;z-index:60;min-width:168px;background:var(--surface);
     border:1px solid var(--border);border-radius:6px;padding:4px 0;
     box-shadow:0 8px 26px rgba(0,0,0,.20)}
.menu[hidden]{display:none}
.menu button{display:block;width:100%;text-align:left;border:none;background:transparent;
     color:var(--ink);font-family:inherit;font-size:13px;padding:8px 14px;
     cursor:pointer;white-space:nowrap}
.menu button:hover:not([disabled]){background:var(--head-bg);color:var(--brand)}
.menu button[disabled]{color:var(--muted);opacity:.55;cursor:default}
.menu button.danger{color:var(--brand)}
.menu button.danger:hover:not([disabled]){background:var(--head-bg);color:var(--brand)}
.menu .sep{height:1px;background:var(--border);margin:4px 0}
.field{margin-bottom:14px}
.field label{display:block;font-size:13px;color:var(--ink2);margin-bottom:6px}
.field .req{color:var(--brand);margin-right:2px}
.field input,.field select,.field textarea{width:100%;padding:8px 10px;
     border:1px solid var(--border);border-radius:6px;background:var(--surface);
     color:var(--ink);font-size:13px;font-family:inherit}
.field textarea{min-height:92px;resize:vertical;
     font-family:ui-monospace,Consolas,monospace}
.field .hint{font-size:12px;color:var(--muted);margin-top:4px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
details.adv{border:1px solid var(--border);border-radius:8px;padding:10px 14px;
     margin-bottom:16px;background:var(--head-bg)}
details.adv summary{cursor:pointer;font-size:13px;color:var(--ink2)}
details.adv .body{padding-top:12px}
.crumbs{font-size:13px;color:var(--ink2);margin-bottom:10px;word-break:break-all}
.crumbs a{color:var(--run)}
.fslist{border:1px solid var(--border);border-radius:8px;max-height:340px;
     overflow:auto}
.fsrow{display:flex;align-items:center;gap:10px;padding:8px 12px;font-size:13px;
     cursor:pointer;border-bottom:1px solid var(--line);color:var(--ink2)}
.fsrow:last-child{border-bottom:none}
.fsrow:hover{background:var(--hover);color:var(--ink)}
.fsrow .ic{width:16px;text-align:center;color:var(--muted);flex:none}
.fsrow .sz{margin-left:auto;color:var(--muted);font-size:12px}
.note{font-size:12px;color:var(--ink2);background:var(--head-bg);
     border:1px solid var(--border);border-radius:6px;padding:8px 10px;
     margin-bottom:10px;white-space:pre-wrap}
.note.warn{color:var(--warn);border-color:var(--warn)}
"""

# 通用 JS：主题读取 / 切换、数字格式化、跑批提交与轮询
JS_COMMON = """
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
function cv(name){
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function chartTheme(){
  /* 曲线配色与 BigQuant 一致：策略收益率=红、相对收益率=橙、基准指数=蓝 */
  return {strat:cv('--strat'), bench:cv('--bench'), excess:cv('--excess'),
          up:cv('--up'), down:cv('--down'), ink:cv('--ink'), ink2:cv('--ink2'),
          muted:cv('--muted'), grid:cv('--grid'), axis:cv('--axis'),
          surface:cv('--surface'), border:cv('--border')};
}
function fmtPct(v, digits=2){ return (v*100).toFixed(digits) + '%'; }
function fmtNum(v, digits=2){
  return v.toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits});
}
/* ── 主题 ── */
function applyTheme(t){
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  localStorage.setItem('paper-theme', t || 'auto');
  const btn = $('#themeBtn');
  if (btn) btn.textContent = (t === 'dark') ? '☾ 深色' : (t === 'light' ? '☀ 浅色' : '◐ 跟随系统');
  if (window.__redrawCharts) window.__redrawCharts();
}
function cycleTheme(){
  const cur = localStorage.getItem('paper-theme') || 'auto';
  applyTheme(cur === 'auto' ? 'light' : (cur === 'light' ? 'dark' : 'auto'));
}
/* URL ?theme=dark|light 优先（便于分享固定主题的链接），否则用本地记忆 */
function bootTheme(){
  const q = new URLSearchParams(location.search).get('theme');
  return q || localStorage.getItem('paper-theme') || 'auto';
}
/* ── 跑批任务 ── */
let __jobTimer = null;
async function submitRun(strategyPath, mode, extra){
  const log = $('#runlog');
  if (log){ log.hidden = false; log.textContent = '提交跑批任务…\\n'; }
  try{
    const r = await fetch('/api/run', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(Object.assign({strategy: strategyPath, mode: mode}, extra||{}))
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    pollJob(j.job_id);
  }catch(e){
    if (log) log.textContent += '提交失败：' + e.message + '\\n';
  }
}
function pollJob(id){
  clearInterval(__jobTimer);
  __jobTimer = setInterval(async () => {
    let j;
    try{
      const r = await fetch('/api/job/' + id);
      j = await r.json();
    }catch(e){ return; }
    const log = $('#runlog');
    if (log) log.textContent = (j.log || []).join('\\n') + (j.status === 'running' ? '\\n…运行中' : '');
    if (j.status === 'running') return;
    clearInterval(__jobTimer);
    if (log){
      log.textContent += '\\n' + (j.status === 'ok'
        ? '✓ ' + (j.result || '完成') : '✗ ' + (j.error || '失败'));
    }
    if (j.status === 'ok') setTimeout(() => location.reload(), 1200);
    else if (j.status === 'stopped') setTimeout(() => location.reload(), 1200);
  }, 1000);
}
/* ── 分页（客户端；对齐平台分页条：‹ 1 2 3 … N › 10条/页 跳至 __ 页）── */
window.__pagers = {};
function initPager(rootId, defaultSize){
  const root = document.getElementById(rootId);
  if (!root) return null;
  const tbody = root.querySelector('tbody');
  const bar = root.parentElement.querySelector('.pager');
  if (!tbody || !bar) return null;
  const all = Array.from(tbody.querySelectorAll('tr'));
  let size = defaultSize || 10, page = 1, filter = null;
  function matched(){ return all.filter(tr => !filter || filter(tr)); }
  function draw(){
    const vis = matched();
    const tp = Math.max(1, Math.ceil(vis.length / size));
    if (page > tp) page = tp;
    const s = (page - 1) * size, e = s + size;
    all.forEach(tr => { tr.style.display = 'none'; });
    vis.slice(s, e).forEach(tr => { tr.style.display = ''; });
    let h = '<button data-go="' + (page - 1) + '"' + (page <= 1 ? ' disabled' : '') + '>‹</button>';
    const nums = [];
    for (let i = 1; i <= tp; i++){
      if (i === 1 || i === tp || Math.abs(i - page) <= 1) nums.push(i);
      else if (nums[nums.length - 1] !== '…') nums.push('…');
    }
    nums.forEach(n => {
      h += (n === '…') ? '<span class="dots">…</span>'
        : '<button data-go="' + n + '"' + (n === page ? ' aria-current="true"' : '') + '>' + n + '</button>';
    });
    h += '<button data-go="' + (page + 1) + '"' + (page >= tp ? ' disabled' : '') + '>›</button>';
    h += '<select class="pg-size">' + [10,20,50,100].map(n =>
      '<option value="' + n + '"' + (n === size ? ' selected' : '') + '>' + n + '条/页</option>').join('') + '</select>';
    h += '<span>跳至</span><input class="pg-jump" type="number" min="1" max="' + tp + '"><span>页</span>';
    bar.innerHTML = h;
    bar.querySelectorAll('button[data-go]').forEach(b => b.onclick = () => { page = +b.dataset.go; draw(); });
    bar.querySelector('.pg-size').onchange = ev => { size = +ev.target.value; page = 1; draw(); };
    bar.querySelector('.pg-jump').onchange = ev => {
      const v = +ev.target.value; if (v >= 1 && v <= tp) { page = v; draw(); }
    };
  }
  const api = { setFilter(f){ filter = f; page = 1; draw(); }, redraw: draw };
  window.__pagers[rootId] = api;
  draw();
  return api;
}
function filterByDate(rootId, day){
  const p = window.__pagers[rootId];
  if (!p) return;
  p.setFilter(day ? (tr => (tr.cells[0].innerText || '').trim().startsWith(day)) : null);
}
/* ── 导出表格为 CSV（对齐平台「下载」按钮）── */
function downloadTable(rootId, filename){
  const root = document.getElementById(rootId);
  const t = root && root.querySelector('table');
  if (!t) return;
  const lines = [];
  t.querySelectorAll('tr').forEach(tr => {
    const cells = Array.from(tr.querySelectorAll('th,td')).map(td => {
      /* 用 fromCharCode 引号：在单引号字符串里嵌字面量引号既要反斜杠又要
         正则转义，很容易写错，而且整段在 Python 三引号里 —— 拆开就没歧义 */
      const q = String.fromCharCode(34);
      const cell = (td.innerText || '').replace(/\s+/g, ' ').trim();
      return q + cell.split(q).join(q + q) + q;   // 内部引号按 CSV 规则翻倍
    });
    if (cells.length) lines.push(cells.join(','));
  });
  const blob = new Blob(['﻿' + lines.join('\\r\\n')], {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
/* ── 通用小工具 ── */
function esc(s){
  /* 正则里的引号同样用 fromCharCode，避免在单引号字符串里写引号 */
  const re = new RegExp('[&<>' + String.fromCharCode(34, 39) + ']', 'g');
  return String(s == null ? '' : s).replace(re, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtBytes(n){
  if (n == null) return '';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  return (n/1048576).toFixed(1) + ' MB';
}
/* ── 弹窗 ── */
function openModal(id){ const m = document.getElementById(id); if (m) m.hidden = false; }
function closeModal(id){ const m = document.getElementById(id); if (m) m.hidden = true; }
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') $$('.modal').forEach(m => m.hidden = true);
});
/* ── 列表页「操作」列的 ⋯ 菜单（对齐平台：立即运行/停止部署任务/编辑代码/删除）── */
let __openMenu = null;
function closeRowMenu(){
  if (__openMenu){ __openMenu.hidden = true; __openMenu = null; }
}
/* 菜单挂在 body 上（不是单元格里）并手动定位：表格外层 .tbl-wrap 是
   overflow:auto，放在单元格里的下拉会被裁掉。贴边时翻到按钮上方/左侧。 */
function openRowMenu(btn, sid, name, hasSource, running){
  const menu = document.getElementById('menu-' + sid);
  if (!menu) return;
  const wasOpen = (__openMenu === menu);
  closeRowMenu();
  if (wasOpen) return;
  const r = btn.getBoundingClientRect();
  menu.hidden = false;                       // 先显示才能量到尺寸
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = r.right - mw, top = r.bottom + 4;
  if (left < 8) left = 8;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
  const runBtn = menu.querySelector('[data-act="run"]');
  if (runBtn) runBtn.disabled = !!running;   // 跑批串行：本策略正在跑就别再提
  const editBtn = menu.querySelector('[data-act="edit"]');
  if (editBtn && !hasSource){
    editBtn.disabled = true;
    editBtn.title = '找不到该策略的源文件（strategies/ 下没有）';
  }
  __openMenu = menu;
}
document.addEventListener('click', e => {
  if (!__openMenu) return;
  /* 点菜单项：交给菜单项自己的 onclick 处理，别在这里关 */
  if (e.target.closest('.menu')) return;
  /* 点 ⋯ 按钮：它是切换开关，打开/关闭由它自己的 onclick 决定。
     少了这一条，点击会冒泡到这里，刚被 onclick 打开的菜单立刻又被关掉 ——
     表现就是「点 ⋯ 没反应」。（曾经真的这样。） */
  if (e.target.closest('.menu-btn')) return;
  closeRowMenu();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeRowMenu(); });
document.addEventListener('scroll', () => closeRowMenu(), true);
window.addEventListener('resize', () => closeRowMenu());
async function menuRun(sid, name){
  closeRowMenu();
  const btn = document.getElementById('runbtn-' + sid);
  if (!confirm('立即运行「' + name + '」？将跑最新可得交易日。')) return;
  runRow(btn, sid);
}
async function menuEdit(sid, name, href){
  closeRowMenu();
  /* 配了 --ide-url 时 href 是 IDE 链接，直接开；没配则是 /api/open，
     由服务端调本机编辑器，要等结果才知道成没成 */
  if (href.indexOf('/api/open') !== 0){ window.open(href, '_blank'); return; }
  try{
    const r = await fetch(href);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    location.href = '/?msg=' + encodeURIComponent(j.message || '已打开');
  }catch(e){ alert('打开编辑器失败：' + e.message); }
}
async function menuStop(sid, name){
  closeRowMenu();
  /* 提示文案不写换行：JS 字符串里的换行要写成 \\n，
     而这段 JS 又在 Python 三引号里，少一个反斜杠就把 JS 拆坏了。
     一句话说完，省掉这层转义。 */
  if (!confirm('停止「' + name + '」的部署任务？该策略将被标记为「已停止」，'
      + '正在跑的批次会在当前交易日跑完后中止；已有的持仓、成交与净值记录不受影响。')) return;
  await rowAction('/api/stop', {strategy_id: sid}, sid, '停止');
}
async function menuDelete(sid, name){
  closeRowMenu();
  if (!confirm('删除「' + name + '」？将删除状态库里该账户的全部数据（持仓/成交/绩效/运行历史），'
      + '并删除 strategies/ 下对应的策略文件与同名 .meta.json。此操作不可恢复。')) return;
  if (!confirm('再次确认：彻底删除「' + name + '」及其策略文件？')) return;
  await rowAction('/api/delete', {strategy_id: sid}, sid, '删除');
}
async function rowAction(url, payload, sid, label){
  const btn = document.getElementById('runbtn-' + sid);
  if (btn){ btn.disabled = true; btn.textContent = label + '中…'; }
  try{
    const r = await fetch(url, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    location.href = '/?msg=' + encodeURIComponent(j.message || (label + '成功'));
  }catch(e){
    alert(label + '失败：' + e.message);
    if (btn){ btn.disabled = false; btn.textContent = '跑批'; }
  }
}
function runRow(btn, sid){
  if (btn){ btn.disabled = true; btn.textContent = '运行中…'; }
  submitRunBySid(sid);
}
async function submitRunBySid(sid){
  const log = document.getElementById('runlog');
  if (log){ log.hidden = false; log.textContent = '提交跑批任务…\\n'; }
  try{
    const r = await fetch('/api/run', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({strategy_id: sid, mode:'auto'})
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    pollJob(j.job_id);
  }catch(e){
    if (log) log.textContent += '提交失败：' + e.message + '\\n';
  }
}
/* ── 提交模拟 第一步：目录浏览 ── */
function openSubmit(){ openModal('mBrowse'); browseTo(''); }
async function browseTo(rel){
  const box = document.getElementById('fslist');
  const crumbs = document.getElementById('fsCrumbs');
  if (!box) return;
  box.innerHTML = '<div class="fsrow"><span class="ic">…</span>加载中…</div>';
  let j;
  try{
    const r = await fetch('/api/browse?path=' + encodeURIComponent(rel || ''));
    j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  }catch(e){
    box.innerHTML = '<div class="fsrow"><span class="ic">✗</span>' + esc(e.message) + '</div>';
    return;
  }
  const parts = (j.rel || '').split('/').filter(Boolean);
  let acc = '', crumb = '<a href="#" data-rel="">' + esc(j.root) + '</a>';
  parts.forEach(p => {
    acc = acc ? acc + '/' + p : p;
    crumb += ' / <a href="#" data-rel="' + esc(acc) + '">' + esc(p) + '</a>';
  });
  crumbs.innerHTML = crumb;
  $$('a[data-rel]', crumbs).forEach(a => a.onclick = ev => {
    ev.preventDefault(); browseTo(a.dataset.rel);
  });

  let rows = '';
  if (j.rel && j.parent !== null && j.parent !== undefined)
    rows += '<div class="fsrow" data-rel="' + esc(j.parent) +
            '"><span class="ic">↑</span>返回上级</div>';
  (j.dirs || []).forEach(d =>
    rows += '<div class="fsrow" data-rel="' + esc(d.rel) + '"><span class="ic">📁</span>' +
            esc(d.name) + '</div>');
  (j.files || []).forEach(f =>
    rows += '<div class="fsrow" data-file="' + esc(f.rel) + '"><span class="ic">📄</span>' +
            '<span class="nm">' + esc(f.name) + '</span>' +
            '<span class="sz">' + fmtBytes(f.size) + '</span></div>');
  if (!rows)
    rows = '<div class="fsrow"><span class="ic">—</span>此目录没有可提交的 .py / .ipynb</div>';
  box.innerHTML = rows;
  $$('.fsrow[data-rel]', box).forEach(el => el.onclick = () => browseTo(el.dataset.rel));
  $$('.fsrow[data-file]', box).forEach(el => el.onclick = () => pickFile(el.dataset.file));
}
/* ── 提交模拟 第二步：解析策略 → 填表 ── */
async function pickFile(rel){
  const box = document.getElementById('fslist');
  box.innerHTML = '<div class="fsrow"><span class="ic">…</span>解析策略文件…</div>';
  let j;
  try{
    const r = await fetch('/api/probe?path=' + encodeURIComponent(rel));
    j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  }catch(e){
    box.innerHTML = '<div class="fsrow"><span class="ic">✗</span>' + esc(e.message) +
                    ' —— <span data-rel="' + esc(rel.split('/').slice(0,-1).join('/')) +
                    '" class="back">返回</span></div>';
    const back = box.querySelector('.back');
    if (back) back.onclick = () => browseTo(back.dataset.rel);
    return;
  }
  fillSubmitForm(j);
  closeModal('mBrowse');
  openModal('mSubmit');
}
function fillSubmitForm(j){
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = v;
  };
  set('fSourceRel', j.rel || '');
  set('fSid', j.strategy_id || '');
  set('fName', j.strategy_name || '');
  set('fCapital', j.capital_base || '');
  set('fBenchmark', j.benchmark || '000300.SH');
  set('fBuy', j.order_price_field_buy || 'open');
  // 预热天数：策略里写了就自动填上；没写则保留默认 60 天（可直接改）
  const wm = document.getElementById('fWarmup');
  if (wm && j.warmup_days != null) wm.value = j.warmup_days;
  const wh = document.getElementById('fWarmupHint');
  if (wh) {
    wh.textContent = (j.warmup_days == null)
      ? ('默认 60 天，可改'
         + (j.warmup_days_hint
            ? '；按源码回看推断需 ≥ ' + j.warmup_days_hint + ' 自然日'
            : ''))
      : '自动取自策略里的 warmup_days';
  }
  set('fSell', j.order_price_field_sell || 'open');
  const fname = document.getElementById('fFileName');
  if (fname) fname.textContent = j.file || '';
  const notes = [];
  (j.notes || []).forEach(n => notes.push(n));
  if (j.missing && j.missing.length) notes.push('待填写：' + j.missing.join('、'));
  const box = document.getElementById('fNotes');
  if (box) box.innerHTML = notes.map(n =>
    '<div class="note' + (n.indexOf('⚠') === 0 ? ' warn' : '') + '">' + esc(n) + '</div>'
  ).join('');
  const msg = document.getElementById('fMsg');
  if (msg) msg.innerHTML = '';
}
async function doSubmit(ev){
  ev.preventDefault();
  const btn = document.getElementById('fSubmitBtn');
  const msg = document.getElementById('fMsg');
  const val = id => { const el = document.getElementById(id); return el ? el.value : ''; };
  // 预热天数是硬性要求：策略里没写就必须由提交人确认
  const warmup = val('fWarmup').trim();
  if (!warmup){
    if (msg) msg.innerHTML = '<div class="note warn">请先填写「预热天数」'
      + '（行情窗口前推自然日）再提交 —— 窗口不够会让因子算成 NaN、'
      + '策略静默不下单。</div>';
    const wm = document.getElementById('fWarmup');
    if (wm) wm.focus();
    return false;
  }
  const body = {
    source: val('fSourceRel'),
    strategy_id: val('fSid').trim(),
    strategy_name: val('fName').trim(),
    capital_base: val('fCapital'),
    benchmark: val('fBenchmark').trim(),
    warmup_days: warmup,
    order_price_field_buy: val('fBuy'),
    order_price_field_sell: val('fBuy'),
    trigger_time: val('fTrigger'),
    input_data: val('fInputData'),
    output_tag: val('fOutputTag'),
  };
  btn.disabled = true; btn.textContent = '提交中…';
  if (msg) msg.innerHTML = '';
  try{
    const r = await fetch('/api/submit', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    closeModal('mSubmit');
    location.href = '/tasks/' + encodeURIComponent(j.strategy_id) + '?job=' + j.job_id;
  }catch(e){
    if (msg) msg.innerHTML = '<div class="note warn">' + esc(e.message) + '</div>';
    btn.disabled = false; btn.textContent = '提交并运行';
  }
}
/* ── 任务页：立即运行 + 实时日志 ── */
async function runTask(path, sid){
  try{
    const r = await fetch('/api/run', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({strategy: path, mode:'auto', strategy_id: sid})
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    location.href = '/tasks/' + encodeURIComponent(sid) + '?job=' + j.job_id;
  }catch(e){ alert('提交失败：' + e.message); }
}
let __taskTimer = null;
function watchJob(jid){
  const view = document.getElementById('liveLog');
  if (!view || !jid) return;
  clearInterval(__taskTimer);
  const tick = async () => {
    let j;
    try{ const r = await fetch('/api/job/' + jid); j = await r.json(); }catch(e){ return; }
    view.textContent = (j.log || []).join('\\n') +
      (j.status === 'running' ? '\\n…运行中' : '');
    if (j.status === 'running') return;
    clearInterval(__taskTimer);
    view.textContent += '\\n' + (j.status === 'ok'
      ? '✓ ' + (j.result || '完成') : '✗ ' + (j.error || '失败'));
    setTimeout(() => location.reload(), 1500);
  };
  tick();
  __taskTimer = setInterval(tick, 1000);
}
/* ── 任务管理器左栏：搜索 + 状态筛选 ── */
function filterTasks(){
  const qEl = document.getElementById('taskSearch');
  const q = (qEl ? qEl.value : '').trim().toLowerCase();
  const tab = document.querySelector('.tabs button[aria-pressed="true"]');
  const st = tab ? tab.dataset.st : '';
  let shown = 0;
  $$('.task').forEach(el => {
    const okQ = !q || (el.dataset.name || '').toLowerCase().indexOf(q) >= 0;
    const okS = !st || el.dataset.status === st;
    const vis = okQ && okS;
    el.style.display = vis ? '' : 'none';
    if (vis) shown++;
  });
  const empty = document.getElementById('taskEmpty');
  if (empty) empty.style.display = shown ? 'none' : '';
}
function pickTab(btn){
  $$('.tabs button').forEach(b => b.setAttribute('aria-pressed',
    b === btn ? 'true' : 'false'));
  filterTasks();
}
/* 深链：?submit 打开文件选择，?submit=<相对路径> 直接打开已预填的提交表单 */
(function(){
  const q = new URLSearchParams(location.search).get('submit');
  if (q === null || !document.getElementById('mBrowse')) return;
  if (q) pickFile(q); else openSubmit();
})();
"""


def page(title: str, body: str, extra_js: str = '', extra_head: str = '') -> bytes:
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)}</title>
<style>{CSS}</style>
<script>/* 尽早打上主题，避免深色模式闪白 */ (function(){{
  var q = new URLSearchParams(location.search).get('theme');
  var s = q || localStorage.getItem('paper-theme');
  if (s && s !== 'auto') document.documentElement.dataset.theme = s;
}})();</script>
{extra_head}
</head><body>
<div class="wrap">{body}</div>
<script>{JS_COMMON}</script>
<script>applyTheme(bootTheme());</script>
<script>{extra_js}</script>
</body></html>""".encode('utf-8')


def theme_button() -> str:
    return ('<button class="btn" id="themeBtn" onclick="cycleTheme()">◐ 跟随系统</button>')


def nav(active: str, ide_url: str = '') -> str:
    """顶部导航（对齐平台：我的交易 / 任务管理 + 提交模拟）。"""
    def link(href: str, label: str, key: str) -> str:
        cur = ' aria-current="page"' if key == active else ''
        return f'<a class="nav-a" href="{href}"{cur}>{h(label)}</a>'

    ide = ('<a class="nav-a" href="/ide" target="_blank">编辑器</a>'
           if ide_url else '')
    return (f'<nav class="nav">{link("/", "我的交易", "index")}'
            f'{link("/tasks", "任务管理", "tasks")}{ide}'
            f'<span class="spacer"></span>'
            f'<button class="btn primary" onclick="openSubmit()">＋ 提交模拟</button>'
            f'</nav>')


def _submit_modals() -> str:
    """「提交模拟」两步弹窗：① 选文件 ② 填参数（字段对齐平台提交任务页）。"""
    return """
<div class="modal" id="mBrowse" hidden>
  <div class="dialog wide">
    <div class="dialog-head"><span>选择策略文件</span><span class="spacer"></span>
      <button class="close-x" onclick="closeModal('mBrowse')">✕</button></div>
    <div class="dialog-body">
      <div class="crumbs" id="fsCrumbs"></div>
      <div class="fslist" id="fslist"></div>
      <p class="hint" style="margin-top:10px">只列出 .py 与 .ipynb。
        选中的文件会被复制到 <code>strategies/</code>，之后一律以那份为准。</p>
    </div>
  </div>
</div>
<div class="modal" id="mSubmit" hidden>
  <div class="dialog">
  <form onsubmit="return doSubmit(event)">
    <div class="dialog-head"><span>提交任务</span>
      <span class="crumb" id="fFileName"></span><span class="spacer"></span>
      <button type="button" class="close-x" onclick="closeModal('mSubmit')">✕</button></div>
    <div class="dialog-body">
      <div id="fNotes"></div>
      <div class="grid2">
        <div class="field"><label><span class="req">*</span>任务类型</label>
          <select disabled><option>日频交易任务</option></select></div>
        <div class="field"><label><span class="req">*</span>任务名称</label>
          <input id="fName" placeholder="如：轮动宝v3"></div>
      </div>
      <div class="field"><label><span class="req">*</span>部署服务器</label>
        <select disabled><option>本地（本项目）</option></select></div>
      <details class="adv"><summary>高级设置 —— 调度时间、数据依赖与输出标签</summary>
        <div class="body">
          <div class="grid3">
            <div class="field"><label>触发时间（时：分）</label>
              <input id="fTrigger" type="time" value="15:30"></div>
            <div class="field"><label>输入数据</label>
              <input id="fInputData" placeholder="如：cn_fund_bar1d"></div>
            <div class="field"><label>输出标签</label>
              <input id="fOutputTag" placeholder="如：papertrading"></div>
          </div>
          <p class="hint">本地跑批由 run_daily.py / 任务计划程序调度，
            触发时间仅作展示与留痕。</p>
        </div>
      </details>
      <div class="grid2">
        <div class="field"><label><span class="req">*</span>策略 ID（状态库键）</label>
          <input id="fSid" placeholder="换 ID = 全新账户"></div>
        <div class="field"><label><span class="req">*</span>初始资金</label>
          <input id="fCapital" type="number" step="1000" placeholder="1000000"></div>
      </div>
      <div class="grid3">
        <div class="field"><label>基准</label>
          <input id="fBenchmark" value="000300.SH"></div>
        <div class="field"><label><span class="req">*</span>预热天数</label>
          <input id="fWarmup" type="number" min="1" step="1" value="60"
            placeholder="自然日">
          <div class="hint" id="fWarmupHint"></div></div>
        <div class="field"><label>成交价</label>
          <select id="fBuy"><option value="open">开盘价</option>
            <option value="close">收盘价</option></select></div>
      </div>
      <input type="hidden" id="fSourceRel">
      <div id="fMsg"></div>
    </div>
    <div class="dialog-foot">
      <button type="button" class="btn" onclick="closeModal('mSubmit')">取消</button>
      <button type="submit" class="btn primary" id="fSubmitBtn">提交并运行</button>
    </div>
  </form>
  </div>
</div>
"""


# ══════════════════════════════════════════════════════════════════
# 列表页
# ══════════════════════════════════════════════════════════════════
def _inst_cell(name: str, code: str) -> str:
    """股票单元格：名称 + 下方灰色代码（对齐平台样式）。"""
    return (f'<div class="inst"><span class="nm">{h(name or "—")}</span>'
            f'<span class="cd">{h(code)}</span></div>')


# 平台列表页「策略名称」列：名称后面跟一枚浅蓝板块标（股票/基金/…）。
# 取值自 strategies.market，认不出就不显示标签，不猜。
_MARKET_TAG = {'cn_stock_fund': '股票', 'cn_stock': '股票', 'cn_fund': '基金'}


def _market_tag(market: Any) -> str:
    text = _MARKET_TAG.get(str(market or '').strip())
    return f'<span class="tag stock">{h(text)}</span>' if text else ''


def _name_cell(name: str, code: str, market: Any = '') -> str:
    """策略名称单元格：名称 + 板块标，下一行灰色 strategy_id（对齐平台列表页）。"""
    return (f'<div class="inst"><span class="nm">{h(name or "—")}'
            f'{_market_tag(market)}</span>'
            f'<span class="cd">{h(code)}</span></div>')


def _table(headers: List[str], rows: List[List[str]], left: int = 1,
           pager: bool = False, empty: str = '暂无记录') -> str:
    if not rows:
        return f'<p class="empty">{h(empty)}</p>'
    ths = ''.join(f'<th class="{"l" if i < left else ""}">{h(x)}</th>'
                  for i, x in enumerate(headers))
    trs = []
    for r in rows:
        tds = ''.join(f'<td class="{"l" if i < left else ""}">{c}</td>'
                      for i, c in enumerate(r))
        trs.append(f'<tr>{tds}</tr>')
    return (f'<div class="tbl-wrap"><table><thead><tr>{ths}</tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></div>'
            + ('<div class="pager"></div>' if pager else ''))


def _date_select(root_id: str, days: List[str]) -> str:
    """「请选择日期」下拉（对齐平台日期筛选，客户端过滤）。"""
    opts = ''.join(f'<option value="{h(d)}">{h(d)}</option>' for d in days)
    return (f'<select class="btn" onchange="filterByDate(\'{h(root_id)}\', this.value)">'
            f'<option value="">请选择日期</option>{opts}</select>')


def jss(v: Any) -> str:
    """把值渲染成可嵌进 HTML 属性的 JS 字面量。

    `json.dumps` 会输出双引号，直接塞进 `onclick="..."` 会把属性截断 ——
    中文策略名（「轮动宝v3」）必然触发，且浏览器静默吞掉，不会报错。
    这里再走一遍 html.escape(quote=True)，双引号变成 `&quot;`，HTML 解析
    后 JS 拿到的仍是合法字面量。
    """
    return html.escape(json.dumps(v, ensure_ascii=False), quote=True)


def _row_menu(r: Dict[str, Any]) -> str:
    """「操作」列的 ⋯ 菜单：立即运行 / 停止部署任务 / 编辑代码 / 删除。

    菜单本体挂在 <body> 上（`_row_menus` 输出），这里只放触发按钮 ——
    表格外层 .tbl-wrap 是 overflow:auto，放在单元格里会被裁掉。
    """
    sid = r['strategy_id']
    return (f'<button class="menu-btn" title="更多操作" '
            f'data-menu="menu-{h(sid)}" '
            f'onclick="openRowMenu(this, {jss(sid)}, '
            f'{jss(r["strategy_name"])}, '
            f'{str(bool(r.get("edit_href"))).lower()}, '
            f'{str(r["status"] == "running").lower()})">⋯</button>')


def _row_menus(rows: List[Dict[str, Any]]) -> str:
    """列表页所有行的 ⋯ 菜单容器（点按钮时才由 JS 显示/定位）。"""
    out = []
    for r in rows:
        sid = r['strategy_id']
        sil, snm = jss(sid), jss(r['strategy_name'])
        edit = r.get('edit_href')
        edit_html = (
            f'<button data-act="edit" onclick="menuEdit({sil}, {snm}, '
            f'{jss(edit)})">编辑代码</button>'
            if edit else '<button data-act="edit" disabled '
                         f'title="找不到该策略的源文件">编辑代码</button>')
        out.append(
            f'<div class="menu" id="menu-{h(sid)}" hidden>'
            f'<button data-act="run" onclick="menuRun({sil}, {snm})">立即运行</button>'
            f'<button onclick="menuStop({sil}, {snm})">停止部署任务</button>'
            f'{edit_html}'
            f'<div class="sep"></div>'
            f'<button class="danger" onclick="menuDelete({sil}, {snm})">删除</button>'
            f'</div>')
    return ''.join(out)


def render_index(rows: List[Dict[str, Any]],
                 files: List[Dict[str, Any]],
                 db_path: str, ide_url: str = '',
                 msg: str = '', msg_kind: str = 'ok') -> bytes:
    head = f"""{nav('index', ide_url)}
<header class="top">
  <h1>我的交易</h1>
  <span class="crumb">状态库 {h(db_path)}</span>
  <span class="spacer"></span>
  <button class="btn" onclick="location.reload()">刷新</button>
  {theme_button()}
</header>"""

    notice = (f'<div class="msg {"ok" if msg_kind == "ok" else "bad"}">'
              f'{h(msg)}</div>' if msg else '')

    if rows:
        rows_data = []
        for r in rows:
            st = r['status']
            last = r.get('last_run') or {}
            last_at = str(last.get('started_at') or '')[:19]
            # 最近运行时间：无记录给「—」；有记录时附带状态徽标（对齐平台该列
            # 带一个运行状态图标）。小时「HH:MM:SS」按平台样式弱化显示。
            if last_at:
                # 最近一次运行结果：对齐平台 —— 成功绿勾、失败红标
                lv = str(last.get('status') or '')
                run_cls = 'done' if lv == 'ok' else ('fail' if lv == 'error' else 'warn')
                run_cell = (f'<div class="inst"><span class="nm">'
                            f'<span class="tag {run_cls}">'
                            f'{h(webdata.task_status_text(last.get("status")))}</span>'
                            f'</span><span class="cd">{h(last_at)}</span></div>')
            else:
                run_cell = '<span class="muted">—</span>'
            # 文案只有「运行中/已停止」两种，配色也必须只有两种 ——
            # 否则同一个词会拿到不同的框色（见 strategy_status_text 注释）
            tag_cls = 'stopped' if st == 'stopped' else 'ok'
            rows_data.append([
                f'<a href="/s/{h(r["strategy_id"])}">'
                f'{_name_cell(r["strategy_name"], r["strategy_id"], r.get("market"))}</a>',
                f'<span class="tag {tag_cls}">{h(webdata.strategy_status_text(st))}</span>',
                f_money(r['portfolio_value']),
                f'<span class="{tone_of(r["cumulative_return"])}">'
                f'{f_pct(r["cumulative_return"])}</span>',
                f'<span class="{tone_of(r["today_return"])}">'
                f'{f_pct(r["today_return"])}</span>',
                f'<span class="{tone_of(r["annualized_return"])}">'
                f'{f_pct(r["annualized_return"])}</span>',
                f_pct(r['max_drawdown'], signed=False),
                f_num(r['sharpe']),
                f_int(r['position_count']),
                f_int(r['pending_count']),
                h(r['first_trading_day'] or '—'),
                run_cell,
                _row_menu(r),
            ])
        table = _table(
            ['策略', '状态', '总资产', '累计收益', '今日收益', '年化收益', '最大回撤',
             '夏普比率', '持仓', '待执行', '开始运行', '最近运行时间', '操作'],
            rows_data, left=1)
    else:
        table = ('<p class="empty">还没有已注册的策略。跑一次 '
                 '<code>python run_daily.py --strategy strategies/xxx.py</code> '
                 '即可注册，或点下方策略文件行的「首跑」。</p>')

    file_rows = []
    for f in files:
        if f['registered'] or f['error']:
            continue
        file_rows.append([
            f'<div class="inst"><span class="nm">{h(f["file"])}</span>'
            f'<span class="cd">{h(f["strategy_id"] or "—")}</span></div>',
            f_int(f['instruments']), f_money(f['capital_base']),
            f'<button class="btn" id="runbtn-{h(f["strategy_id"])}" '
            f'onclick="quickRun(this, {jss(f["path"])})">首跑</button>'])
    err_rows = [[h(f['file']), f'<span class="up">{h(f["error"])}</span>', '', '']
                for f in files if f['error']]

    files_section = ''
    if file_rows or err_rows:
        files_section = f"""<div class="section">
  <div class="section-head">
    <h2>未注册的策略文件</h2>
    <span class="range-stat">在 <code>strategies/</code> 下发现、但状态库里还没有账户；「首跑」= 用初始资金开户并跑最新交易日</span>
  </div>
  {_table(['文件 / strategy_id', '标的数', '初始资金', '操作'],
          file_rows + err_rows, left=1)}
</div>"""

    # 顶部「模拟交易总览」——版式复刻平台列表页：左列「标题 / 日期」上下堆叠，
    # 右侧是**同一行**的指标行（标签 + 数值并排）；计数条另起一行放在卡片上方。
    # 合并口径见 webdata.portfolio_overview，改动前先读那里的 docstring。
    summary = ''
    if rows:
        ov = webdata.portfolio_overview(rows)

        def _sm(label: str, text: str, tone: str = '') -> str:
            return (f'<div class="sm"><span class="k">{h(label)}</span>'
                    f'<span class="v {tone}">{text}</span></div>')

        summary = (
            '<div class="counters">'
            f'<span>策略总数（{ov["count"]}个）</span>'
            f'<span>运行的策略（{ov["running"]}个）</span>'
            '</div>'
            '<div class="section summary"><div class="summary-head">'
            '<div class="summary-id">'
            '<span class="summary-title">模拟交易总览</span>'
            f'<span class="summary-date">{h(ov["day"] or "—")}</span>'
            '</div>'
            '<div class="summary-metrics">'
            + _sm('今日收益', f_pct(ov['today_return']),
                  tone_of(ov['today_return']))
            + _sm('近一周收益', f_pct(ov['week_return']),
                  tone_of(ov['week_return']))
            + _sm('累计收益', f_pct(ov['cumulative_return']),
                  tone_of(ov['cumulative_return']))
            + _sm('最大回撤', f_pct(ov['max_drawdown'], signed=False))
            + _sm('夏普比率', f_num(ov['sharpe']))
            + '</div></div></div>')

    body = head + notice + summary \
           + f'<div class="section"><div class="section-head">' \
             f'<h2>运行中的策略</h2>' \
             f'</div>{table}</div>' + files_section \
           + '<pre id="runlog" class="runlog" hidden></pre>' \
           + _row_menus(rows) \
           + _submit_modals()

    js = """
/* 未注册策略文件行的「首跑」：本地路径直接跑，无需账户 */
function quickRun(btn, path){
  if (!path){ alert('该策略缺少源文件路径（早期注册的账户），请用 run_daily.py 跑批'); return; }
  if (!confirm('用初始资金开户并跑最新交易日？')) return;
  if (btn) { btn.disabled = true; btn.textContent = '运行中…'; }
  submitRun(path, 'auto', {});
}
function runRow(btn, sid){
  if (document.getElementById('menu-' + sid)) closeRowMenu();
  if (btn){ btn.disabled = true; btn.textContent = '运行中…'; }
  submitRunBySid(sid);
}
"""
    return page('我的交易 · 模拟盘看板', body, js)


# ══════════════════════════════════════════════════════════════════
# 任务管理器（对齐平台「任务管理」：左列表 + 右详情/日志）
# ══════════════════════════════════════════════════════════════════
def _task_card(r: Dict[str, Any], selected_id: str) -> str:
    """左栏一张任务卡（状态点 + 状态 + 任务名 + 时间/规格 + 右箭头）。"""
    sid = r['strategy_id']
    last = r.get('last_run') or {}
    st = str(last.get('status') or 'pending')
    when = str(last.get('started_at') or '')[:19]
    cur = ' aria-current="true"' if sid == selected_id else ''
    return (f'<a class="task" href="/tasks/{h(sid)}" data-status="{h(st)}" '
            f'data-name="{h(r["strategy_name"])} {h(sid)}"{cur}>'
            f'<div class="row1"><span class="dot {h(st)}"></span>'
            f'<span class="st {h(st)}">{h(webdata.task_status_text(st))}</span>'
            f'<span class="kind">日频交易任务</span></div>'
            f'<div class="name">{h(r["strategy_name"])}</div>'
            f'<div class="meta"><span>🕐 {h(when or "—")}</span>'
            f'<span>🖥 本地</span><span>📈 {h(r["run_days"])} 个交易日</span></div>'
            f'<span class="chev">›</span></a>')


def render_tasks(rows: List[Dict[str, Any]],
                 detail: Optional[Dict[str, Any]],
                 runs: List[Dict[str, Any]],
                 db_path: str, ide_url: str = '', job_id: str = '',
                 selected_id: str = '') -> bytes:
    counts = {'': len(rows), 'running': 0, 'ok': 0, 'error': 0}
    cards = []
    for r in rows:
        st = str((r.get('last_run') or {}).get('status') or 'pending')
        if st in counts:
            counts[st] += 1
        cards.append(_task_card(r, selected_id))

    tab_html = []
    for key, label in (('', '全部'), ('running', '运行中'),
                       ('ok', '已完成'), ('error', '失败')):
        pressed = 'true' if key == '' else 'false'
        tab_html.append(
            f'<button data-st="{key}" aria-pressed="{pressed}" '
            f'onclick="pickTab(this)">{h(label)}'
            f'<span class="cnt">{counts[key]}</span></button>')

    left = f"""<div class="tasks-left">
  <div class="tasks-search">
    <input id="taskSearch" placeholder="搜索任务名称" oninput="filterTasks()">
  </div>
  <div class="tabs">{"".join(tab_html)}</div>
  {"".join(cards) or '<p class="empty">还没有任务，点右上角「＋ 提交模拟」新建</p>'}
  <p class="empty" id="taskEmpty" style="display:none">没有匹配的任务</p>
</div>"""

    if detail is None:
        right = ('<div class="tasks-right"><div class="section">'
                 '<p class="empty">从左侧选择一个任务，这里会显示它的运行状态、'
                 '运行历史与日志。</p></div></div>')
    else:
        m = detail['meta']
        sid = m['strategy_id']
        last = runs[0] if runs else None
        status = str((last or {}).get('status') or 'pending')
        when = str((last or {}).get('started_at') or '')[:19]
        params = dict((last or {}).get('params') or {})

        src = str(m.get('source') or '')
        if src:
            run_btn = (f'<button class="btn primary" onclick="runTask('
                       f'{json.dumps(src, ensure_ascii=False)}, '
                       f'{json.dumps(sid, ensure_ascii=False)})">▶ 立即运行</button>')
        else:
            run_btn = ('<button class="btn" disabled '
                       'title="该账户缺少源文件路径，请用 run_daily.py 跑批">'
                       '▶ 立即运行</button>')

        chips = [
            f'<span class="chip plain"><span class="dot {h(status)}"></span>'
            f'{h(webdata.task_status_text(status))}</span>',
            f'<span class="chip">🏷 {h(sid)}</span>',
            f'<span class="chip">🕐 {h(when or "—")}</span>',
            '<span class="chip">🖥 本地</span>',
        ]
        if params.get('input_data'):
            chips.append(f'<span class="chip">📄 {h(params["input_data"])}</span>')
        if params.get('output_tag'):
            chips.append(f'<span class="chip">{h(params["output_tag"])}</span>')
        if params.get('trigger_time'):
            chips.append(f'<span class="chip">⏱ {h(params["trigger_time"])}</span>')
        chips.append('<span class="chip plain">⊞ 日频交易任务</span>')
        chips.append(f'<span class="chip plain"><a href="/s/{h(sid)}">'
                     f'查看模拟交易详情</a></span>')

        hist_rows = [[
            h(r['run_id']), h(r['kind']),
            f'<span class="st {h(r["status"])}">{h(r["status_text"])}</span>',
            h(r['trading_day'] or '—'), h(r['started_at'] or '—'),
            h(r['finished_at'] or '—'),
            h(r['summary'] or r['error'] or '—'),
        ] for r in runs]
        hist = _table(['运行 ID', '类型', '状态', '交易日', '开始', '结束', '摘要'],
                      hist_rows, left=1, empty='还没有运行记录')

        if job_id:
            log_html = (f'<pre class="logview" id="liveLog"></pre>'
                        f'<script>watchJob({json.dumps(job_id)});</script>')
        elif last and last.get('log'):
            log_html = f'<pre class="logview">{h(chr(10).join(last["log"]))}</pre>'
        else:
            log_html = '<pre class="logview"></pre>'

        right = f"""<div class="tasks-right">
  <div class="task-head"><h1>{h(m['strategy_name'])}</h1>
    <span class="spacer"></span>{run_btn}</div>
  <div class="chips">{"".join(chips)}</div>
  <div class="section"><div class="section-head"><h2>运行历史</h2>
    <span class="range-stat">最近 {len(runs)} 次</span></div>{hist}</div>
  <div class="section"><div class="section-head"><h2>运行日志</h2>
    <span class="range-stat">运行中自动刷新</span></div>{log_html}</div>
</div>"""

    head = f"""{nav('tasks', ide_url)}
<header class="top">
  <h1>任务管理</h1>
  <span class="crumb">状态库 {h(db_path)}</span>
  <span class="spacer"></span>
  <button class="btn" onclick="location.reload()">刷新</button>
  {theme_button()}
</header>"""
    body = head + f'<div class="tasks">{left}{right}</div>' + _submit_modals()
    return page('任务管理 · 模拟盘看板', body)


# ══════════════════════════════════════════════════════════════════
# 详情页
# ══════════════════════════════════════════════════════════════════
def _metric(label: str, value: str, tone: str = '') -> str:
    return (f'<div class="metric"><div class="v {tone}">{value}</div>'
            f'<div class="k">{h(label)}</div></div>')


def _two_line(a: str, b: str) -> str:
    return (f'<div class="inst"><span class="nm">{a}</span>'
            f'<span class="cd">{b}</span></div>')


def _side_cell(side: str) -> str:
    """「操作」列单元格：对齐平台用单字「买」/「卖」，买红卖绿（A 股习惯）。"""
    tone = 'up' if side == webdata.SIDE_BUY else 'down'
    return f'<span class="{tone}">{h(side)}</span>'


def _hhmm(order_dt: str) -> str:
    """计划单时间列（HH:MM），取自计划单自己的 order_dt。

    本地日频买卖都按 open 撮合，写入时统一为「T+1 09:30」（见
    paper.state.save_state）；此前这里写死 15:00（平台按收盘价撮合的口径），
    与本策略的开盘撮合不符。取不到时显示 '—'，不猜一个时间出来。
    """
    s = str(order_dt or '').strip()
    if ' ' in s:
        s = s.split(' ', 1)[1]
    return s[:5] if len(s) >= 5 else '—'


def render_detail(d: Dict[str, Any], series: Dict[str, Any],
                  db_path: str, ide_url: str = '') -> bytes:
    m = d['meta']
    sid = m['strategy_id']
    pv = m['portfolio_value'] or m['capital_base']

    head = f"""{nav('index', ide_url)}
<header class="top">
  <h1>{h(m['strategy_name'])}</h1>
  <span class="tag">{h(sid)}</span>
  <span class="spacer"></span>
  <a class="btn" href="/">← 返回</a>
  <button class="btn primary" onclick="toggleRun()">跑批</button>
  {theme_button()}
</header>
<div class="statusline">
  <span class="run">▶ {h('运行中' if m['status'] == 'running' else m['status'])}</span>
  <span>基准 {h(m['benchmark'])}</span><span class="dot">|</span>
  <span>{f_int(m['run_days'])} 个交易日</span><span class="dot">|</span>
  <span>最新 {h(m['last_trading_day'] or '—')}</span>
</div>
<div class="runbox" id="runbox" hidden>
  <form class="runform" onsubmit="return onRun(event)">
    <label><input type="radio" name="mode" value="auto" checked> 最新交易日</label>
    <label><input type="radio" name="mode" value="date"> 指定日
      <input type="date" name="date"></label>
    <label><input type="radio" name="mode" value="range"> 区间
      <input type="date" name="from"> ~ <input type="date" name="to"></label>
    <button class="btn primary" type="submit">开始跑批</button>
  </form>
  <pre id="runlog" class="runlog" hidden></pre>
</div>"""

    # ── 指标行（对齐平台：大号数值 + 下方灰标签，无卡片框）──
    metrics = ''.join([
        _metric('累计收益', f_pct(m['cumulative_return']),
                tone_of(m['cumulative_return'])),
        _metric('今日收益', f_pct(m['today_return']), tone_of(m['today_return'])),
        _metric('年化收益', f_pct(m['annualized_return']),
                tone_of(m['annualized_return'])),
        _metric('最大回撤', f_pct(m['max_drawdown'], signed=False)),
        _metric('夏普比率', f_num(m['sharpe'])),
        _metric('交易胜率', f_pct(m['win_percent'], signed=False)),
        _metric('开始运行日期', h(m['first_trading_day'] or '—')),
    ])
    metrics_sub = ''.join([
        f'<span>总资产 <b>{f_money(m["portfolio_value"])}</b></span>',
        f'<span>基准 <b>{h(m["benchmark"])}</b></span>',
        f'<span>α <b>{f_num(m["alpha"])}</b></span>',
        f'<span>β <b>{f_num(m["beta"])}</b></span>',
        f'<span>信息比 <b>{f_num(m["ir"])}</b></span>',
        f'<span>波动率 <b>{f_pct(m["volatility"], signed=False)}</b></span>',
        f'<span>盈亏比 <b>{f_num(m["pnl_ratio"])}</b></span>',
        f'<span>持仓 <b>{f_int(m["position_count"])}</b></span>',
        f'<span>待执行计划单 <b>{f_int(m["pending_count"])}</b></span>',
    ])

    # ── 计划交易（T+1 委托清单）──
    if d['planned']:
        plan_rows = [[
            _hhmm(p['order_dt']),
            _inst_cell(p['name'], p['instrument']),
            _side_cell(p['side']),
            _two_line(f_int(p['order_qty']),
                      f_pct(p['order_qty'] * p['order_price'] / pv, signed=False)
                      if pv else '—'),
            f_num(p['order_price'], 3),
            h('市价'),
        ] for p in d['planned']]
        plan_sub = (f"执行日 {h(' / '.join(d['planned_days']))} · 共 "
                    f"{len(d['planned'])} 笔（先卖后买）")
    else:
        plan_rows = []
        plan_sub = '下一交易日无委托（空清单是明确信号：今天不动）'
    plan_section = f"""<div class="section">
  <div class="section-head">
    <h2>计划交易{('（' + h(d['planned_days'][0]) + '）') if d['planned_days'] else ''}</h2>
    <span class="range-stat">{plan_sub}</span>
    <div class="grow"></div>
    <button class="btn dl" onclick="downloadTable('planTable','计划交易.csv')">下载</button>
  </div>
  <div id="planTable">{_table(['时间', '股票', '操作', '数量/仓位', '收盘价', '委托类型'],
                              plan_rows, left=2, empty=plan_sub)}</div>
</div>"""

    # ── 收益率走势 ──
    trend_section = f"""<div class="section">
  <div class="section-head"><h2>收益率走势</h2></div>
  <div class="rangebar">
    <div class="chips" id="rangeChips">
      <button class="chip" data-range="1m" aria-pressed="false">1月</button>
      <button class="chip" data-range="3m" aria-pressed="false">3月</button>
      <button class="chip" data-range="6m" aria-pressed="false">6月</button>
      <button class="chip" data-range="1y" aria-pressed="false">1年</button>
      <button class="chip" data-range="all" aria-pressed="true">全部</button>
    </div>
    <span class="range-stat">区间收益 <b id="rangeRet">—</b></span>
    <span class="range-stat">区间最大回撤 <b id="rangeDd">—</b></span>
    <div class="grow"></div>
    <span class="range-stat">基准指数 {h(m['benchmark'])}</span>
  </div>
  <div class="chart" id="navChart"></div>
  <div class="chart-fallback" id="navFallback">
    ECharts 未能加载（需要联网访问 CDN）——下方表格视图仍可读全部数值。
  </div>
  <details class="tv"><summary>表格视图（逐日数值）</summary>
    <div id="navTable" style="margin-top:10px"></div>
  </details>
  <div class="section-head" style="margin:26px 0 6px">
    <h2 style="font-size:14px">回撤</h2>
    <span class="range-stat">逐日回撤（相对区间内历史峰值，创新高归零）；
      虚线为平台滚动口径的最大回撤</span>
  </div>
  <div class="chart dd" id="ddChart"></div>
</div>"""

    # ── 持仓详情（历史每日持仓，新→旧）──
    pos_rows = [[
        h(p['trading_day']),
        _inst_cell(p['name'], p['instrument']),
        f_num(p['cost_price'], 3),
        f_int(p['current_qty']),
        f_int(p['hold_days']),
        f_pct(p['position_ratio'], signed=False) if p['position_ratio'] else '—',
        f_num(p['last_price'], 3),
        f'¥ {f_num(p["market_value"], 0)}',
        f'<span class="{tone_of(p["position_pnl"])}">{f_num(p["position_pnl"], 2)}'
        f' / {f_pct(p["profit_ratio"])}</span>',
    ] for p in d['positions']]
    pos_days = sorted({p['trading_day'] for p in d['positions']}, reverse=True)
    pos_section = f"""<div class="section">
  <div class="section-head">
    <h2>持仓详情</h2>
    <div class="grow"></div>
    <div class="ctl">{_date_select('posTable', pos_days)}
      <button class="btn dl" onclick="downloadTable('posTable','持仓详情.csv')">下载</button></div>
  </div>
  <div id="posTable">{_table(['日期时间', '股票', '成本价', '持仓数量', '持仓天数',
                              '仓位占比', '当日收盘价', '当日市值', '浮动收益/浮动收益率'],
                             pos_rows, left=2, pager=True)}</div>
</div>"""

    # ── 交易详情 ──
    trade_rows = [[
        h(t['trading_day'] + (' ' + t['filled_dt'][11:19]
                              if len(t['filled_dt']) > 10 else '')),
        _inst_cell(t['name'], t['instrument']),
        _side_cell(t['side']),
        f_int(t['filled_qty']),
        f'¥ {f_num(t["filled_price"], 3)}',
        f'¥ {f_num(t["filled_money"], 0)}',
        f_num(t['commission'], 2),
        f_int(t['order_qty']),
        h(t['status_text']),
    ] for t in d['trades']]
    trade_days = sorted({t['trading_day'] for t in d['trades']}, reverse=True)
    trade_section = f"""<div class="section">
  <div class="section-head">
    <h2>交易详情</h2>
    <div class="grow"></div>
    <div class="ctl">{_date_select('tradeTable', trade_days)}
      <button class="btn dl" onclick="downloadTable('tradeTable','交易详情.csv')">下载</button></div>
  </div>
  <div id="tradeTable">{_table(['日期时间', '股票', '操作', '成交量', '成交均价',
                                '成交额', '手续费', '委托数量', '委托状态'],
                               trade_rows, left=2, pager=True)}</div>
</div>"""

    # ── 委托记录 ──
    order_rows = [[
        h(o['trading_day']),
        _inst_cell(o['name'], o['instrument']),
        _side_cell(o['side']),
        f_int(o['order_qty']), f_int(o['filled_qty']),
        f_num(o['order_price'], 3), f_num(o['average_price'], 3),
        h(o['status_text']), h(o['status_msg']),
    ] for o in d['orders']]
    order_section = f"""<div class="section">
  <div class="section-head">
    <h2>委托记录</h2>
    <span class="range-stat">共 {len(d['orders'])} 笔（含未成交/撤单，最新在前）</span>
    <div class="grow"></div>
    <button class="btn dl" onclick="downloadTable('orderTable','委托记录.csv')">下载</button>
  </div>
  <div id="orderTable">{_table(['交易日', '股票', '操作', '委托量', '成交量', '委托价',
                                '成交均价', '状态', '说明'], order_rows,
                               left=2, pager=True)}</div>
</div>"""

    # ── 日志 ──
    log_days: Dict[str, List[str]] = {}
    for lg in d['logs']:
        cls = ' lv' if lg['level'] not in ('INFO', '') else ''
        log_days.setdefault(lg['trading_day'], []).append(
            f'<div class="log-line{cls}">[{h(lg["level"])}] {h(lg["msg"])}</div>')
    logs_html = ''.join(
        f'<div class="log-day"><div class="d">{h(day)}</div>{"".join(lines)}</div>'
        for day, lines in log_days.items()) or '<p class="empty">暂无日志</p>'
    log_section = f"""<div class="section">
  <div class="section-head"><h2>运行日志</h2>
    <span class="range-stat">每日跑批留痕（最新在前）</span></div>
  <details class="tv"><summary>展开 / 收起</summary>
    <div style="margin-top:10px">{logs_html}</div></details>
</div>"""

    body = (head + f'<div class="metrics">{metrics}</div>'
            + f'<div class="metrics-sub">{metrics_sub}</div>'
            + plan_section + trend_section + pos_section + trade_section
            + order_section + log_section)

    series_json = json.dumps(
        dict(dates=series['dates'], strat=series['strat'], bench=series['bench'],
             excess=series['excess'], drawdown=series['drawdown'],
             rows=series['rows'], range_return=series['range_return'],
             range_max_dd=series['range_max_dd'], max_dd=series['max_dd']),
        ensure_ascii=False, allow_nan=False).replace('</', '<\\/')

    js = f"""
const SERIES = JSON.parse(document.getElementById('seriesData').textContent);
let navChart = null, ddChart = null;

function toggleRun(){{
  const box = $('#runbox');
  box.hidden = !box.hidden;
  if (!box.hidden) box.scrollIntoView({{behavior:'smooth', block:'nearest'}});
}}
function onRun(ev){{
  ev.preventDefault();
  const f = ev.target;
  const mode = (f.querySelector('input[name=mode]:checked') || {{}}).value || 'auto';
  const extra = {{}};
  if (mode === 'date'){{
    extra.date = f.querySelector('input[name=date]').value;
    if (!extra.date) {{ alert('请选择日期'); return false; }}
  }} else if (mode === 'range'){{
    extra.date_from = f.querySelector('input[name=from]').value;
    extra.date_to = f.querySelector('input[name=to]').value;
    if (!extra.date_from || !extra.date_to) {{ alert('请选择区间'); return false; }}
  }}
  submitRun({json.dumps(m['source'] or '', ensure_ascii=False)}, mode, extra);
  return false;
}}
function navOption(){{
  const t = chartTheme();
  const pctAxis = {{color: t.muted, fontSize: 11,
                   formatter: v => (Number(v) * 100).toFixed(0) + '%'}};
  return {{
    animation: false,
    grid: {{left: 6, right: 84, top: 40, bottom: 58, containLabel: true}},
    legend: {{data: ['策略收益率', '相对收益率', '基准指数'], top: 0, left: 0,
             itemWidth: 14, itemHeight: 2, itemGap: 18,
             textStyle: {{color: t.ink2, fontSize: 13}}}},
    tooltip: {{
      trigger: 'axis', backgroundColor: t.surface, borderColor: t.border,
      textStyle: {{color: t.ink, fontSize: 12}}, padding: [8, 10],
      axisPointer: {{type: 'line', lineStyle: {{color: t.axis}}}},
      valueFormatter: v => (v == null ? '—' : (Number(v) * 100).toFixed(2) + '%')
    }},
    xAxis: {{
      type: 'category', data: SERIES.dates, boundaryGap: false,
      axisLine: {{lineStyle: {{color: t.axis}}}}, axisTick: {{show: false}},
      axisLabel: {{color: t.muted, fontSize: 11}}
    }},
    yAxis: {{
      type: 'value', scale: true, axisLine: {{show: false}}, axisTick: {{show: false}},
      axisLabel: pctAxis, splitLine: {{lineStyle: {{color: t.grid, type: 'solid'}}}}
    }},
    /* 只留滑条，不要 type:'inside' —— inside 会把鼠标滚轮变成缩放、拖拽变成
       平移，鼠标在图上滚一下就换区间。区间切换统一走上方 1月/3月/6月/1年/全部 */
    dataZoom: [
      {{type: 'slider', height: 16, bottom: 8, borderColor: 'transparent',
        backgroundColor: 'transparent', fillerColor: 'transparent',
        handleStyle: {{color: t.muted}}, textStyle: {{color: t.muted, fontSize: 10}}}}
    ],
    series: [
      {{name: '策略收益率', type: 'line', data: SERIES.strat, showSymbol: false,
        lineStyle: {{width: 2, color: t.strat}}, itemStyle: {{color: t.strat}},
        areaStyle: {{color: t.strat, opacity: 0.10}}, z: 3,
        emphasis: {{focus: 'series'}},
        endLabel: {{show: true, color: t.strat, fontSize: 11,
                   formatter: p => (Number(p.value) * 100).toFixed(2) + '%'}}}},
      {{name: '相对收益率', type: 'line', data: SERIES.excess, showSymbol: false,
        lineStyle: {{width: 2, color: t.excess}}, itemStyle: {{color: t.excess}}, z: 2,
        emphasis: {{focus: 'series'}}}},
      {{name: '基准指数', type: 'line', data: SERIES.bench, showSymbol: false,
        lineStyle: {{width: 2, color: t.bench}}, itemStyle: {{color: t.bench}}, z: 1,
        emphasis: {{focus: 'series'}}}}
    ]
  }};
}}

function ddOption(){{
  const t = chartTheme();
  return {{
    animation: false,
    grid: {{left: 6, right: 84, top: 14, bottom: 26, containLabel: true}},
    tooltip: {{
      trigger: 'axis', backgroundColor: t.surface, borderColor: t.border,
      textStyle: {{color: t.ink, fontSize: 12}}, padding: [8, 10],
      axisPointer: {{type: 'line', lineStyle: {{color: t.axis}}}},
      valueFormatter: v => (v == null ? '—' : (Number(v) * 100).toFixed(2) + '%')
    }},
    xAxis: {{
      type: 'category', data: SERIES.dates, boundaryGap: false,
      axisLine: {{lineStyle: {{color: t.axis}}}}, axisTick: {{show: false}},
      axisLabel: {{color: t.muted, fontSize: 11}}
    }},
    yAxis: {{
      type: 'value', max: 0, axisLine: {{show: false}}, axisTick: {{show: false}},
      axisLabel: {{color: t.muted, fontSize: 11,
                  formatter: v => (Number(v) * 100).toFixed(1) + '%'}},
      splitLine: {{lineStyle: {{color: t.grid, type: 'solid'}}}}
    }},
    series: [{{
      name: '回撤', type: 'line', data: SERIES.drawdown, showSymbol: false,
      lineStyle: {{width: 2, color: t.strat}}, itemStyle: {{color: t.strat}},
      areaStyle: {{color: t.strat, opacity: 0.14}},
      markLine: (SERIES.max_dd > 0) ? {{
        silent: true, symbol: 'none',
        lineStyle: {{type: 'dashed', color: t.muted, width: 1}},
        label: {{formatter: '最大回撤 ' + (SERIES.max_dd * 100).toFixed(2) + '%',
                color: t.muted, fontSize: 11, position: 'insideEndTop'}},
        data: [{{yAxis: -SERIES.max_dd}}]
      }} : undefined
    }}]
  }};
}}

function navTableHtml(){{
  const rows = SERIES.rows.map(r => `<tr>
    <td class="l">${{r.trading_day}}</td>
    <td>${{fmtNum(r.portfolio_value)}}</td>
    <td>${{fmtPct(r.today_return)}}</td>
    <td>${{fmtPct(r.cumulative_return)}}</td>
    <td>${{fmtPct(r.benchmark_cum_return)}}</td>
    <td>${{fmtPct(r.excess_cum_return)}}</td>
    <td>${{fmtPct(r.drawdown)}}</td></tr>`).join('');
  return `<div class="tbl-wrap" style="max-height:380px"><table>
    <thead><tr><th class="l">交易日</th><th>总资产</th><th>日收益</th>
    <th>累计收益</th><th>基准累计</th><th>相对收益</th><th>回撤</th></tr></thead>
    <tbody>${{rows}}</tbody></table></div>`;
}}

function drawCharts(){{
  if (!window.echarts) return;
  if (navChart) navChart.dispose();
  if (ddChart) ddChart.dispose();
  navChart = echarts.init($('#navChart'), null, {{renderer: 'canvas'}});
  ddChart = echarts.init($('#ddChart'), null, {{renderer: 'canvas'}});
  navChart.setOption(navOption());
  ddChart.setOption(ddOption());
  echarts.connect([navChart, ddChart]);
}}
window.__redrawCharts = drawCharts;

function updateRangeStat(){{
  const ret = SERIES.range_return, dd = SERIES.range_max_dd;
  const el1 = $('#rangeRet'), el2 = $('#rangeDd');
  if (el1){{
    el1.textContent = (ret == null) ? '—' : fmtPct(ret);
    el1.className = (ret > 0) ? 'up' : (ret < 0 ? 'down' : '');
  }}
  if (el2) el2.textContent = (dd == null) ? '—' : fmtPct(dd);
}}

async function loadSeries(range){{
  const r = await fetch('/api/series/{h(sid)}?range=' + range);
  const j = await r.json();
  SERIES.dates = j.dates; SERIES.strat = j.strat; SERIES.bench = j.bench;
  SERIES.excess = j.excess; SERIES.drawdown = j.drawdown; SERIES.rows = j.rows;
  SERIES.range_return = j.range_return; SERIES.range_max_dd = j.range_max_dd;
  SERIES.max_dd = j.max_dd;
  $('#navTable').innerHTML = navTableHtml();
  updateRangeStat();
  drawCharts();
}}

$$('#rangeChips .chip').forEach(b => b.addEventListener('click', () => {{
  $$('#rangeChips .chip').forEach(x => x.setAttribute('aria-pressed', 'false'));
  b.setAttribute('aria-pressed', 'true');
  loadSeries(b.dataset.range);
}}));

/* 初始区间可用 ?range=1m|3m|6m|1y|all 指定（便于分享/收藏固定视图） */
(function(){{
  const q = new URLSearchParams(location.search).get('range');
  if (!q) return;
  const chip = $$('#rangeChips .chip').find(b => b.dataset.range === q);
  if (!chip) return;
  $$('#rangeChips .chip').forEach(x => x.setAttribute('aria-pressed', 'false'));
  chip.setAttribute('aria-pressed', 'true');
  loadSeries(q);
}})();

window.addEventListener('resize', () => {{
  if (navChart) navChart.resize();
  if (ddChart) ddChart.resize();
}});

$('#navTable').innerHTML = navTableHtml();
updateRangeStat();
if (window.echarts) drawCharts();
else $('#navFallback').style.display = 'block';
initPager('posTable');
initPager('tradeTable');
initPager('orderTable');
"""
    body += f'<script id="seriesData" type="application/json">{series_json}</script>'
    extra_head = (f'<script src="{ECHARTS_CDN}" onerror="window.__echartsFailed=1"></script>')
    return page(f'{m["strategy_name"]} · 模拟盘看板', body, js, extra_head)


# ══════════════════════════════════════════════════════════════════
# 跑批任务管理（同一时刻只允许一个）
# ══════════════════════════════════════════════════════════════════
class JobManager:
    """把 run_daily.py 的跑批流程放到后台线程执行，并保留输出供页面轮询。

    每次任务都会在状态库 task_runs 留一条记录（running → ok/error），
    「任务管理」页据此显示状态徽标与运行历史；运行中还能轮询实时日志。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._current: Optional[str] = None

    def _now(self) -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def submit(self, strategy_path: str, mode: str = 'auto',
               date: Optional[str] = None, date_from: Optional[str] = None,
               date_to: Optional[str] = None, kind: str = 'run',
               strategy_id: str = '',
               params: Optional[Dict[str, Any]] = None) -> str:
        if not strategy_path or not Path(strategy_path).exists():
            raise RuntimeError(f'策略文件不存在: {strategy_path}')
        with self._lock:
            if self._current and self._jobs[self._current]['status'] == 'running':
                raise RuntimeError('已有跑批任务在执行中，请等它结束')
            jid = uuid.uuid4().hex[:12]
            self._jobs[jid] = dict(
                id=jid, status='running', log=[], result=None, error=None,
                strategy_path=str(strategy_path), mode=mode,
                strategy_id=str(strategy_id or ''), kind=kind,
                cancel=False,
                started_at=self._now(), finished_at=None)
            self._current = jid
        threading.Thread(target=self._run, daemon=True,
                         args=(jid, strategy_path, mode, date, date_from,
                               date_to, kind, params or {})).start()
        return jid

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self._jobs.get(job_id)

    def cancel(self, strategy_id: str) -> bool:
        """给正在跑的该策略任务置中止标记。

        跑批线程会在下一个检查点抛 RunStopped；若这个 job 还没跑到能确定
        strategy_id 那一步（刚 submit、spec 还没加载），则按提交时记下的
        strategy_id 兜底匹配（看板列表页的「立即运行」就是带 id 提交的）。
        """
        sid = str(strategy_id or '')
        with self._lock:
            for job in self._jobs.values():
                if job['status'] != 'running':
                    continue
                if sid and str(job.get('strategy_id') or '') == sid:
                    job['cancel'] = True
                    return True
        return False

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._jobs.values())

    def _run(self, jid: str, path: str, mode: str, date: Optional[str],
             date_from: Optional[str], date_to: Optional[str],
             kind: str, params: Dict[str, Any]) -> None:
        job = self._jobs[jid]
        buf = StringIO()
        sid = str(job.get('strategy_id') or '')
        store = None
        try:
            store = StateStore(self.db_path)
            with redirect_stdout(buf):
                spec = StrategySpec.load(path)
                sid = spec.strategy_id
                job['strategy_id'] = sid
                store.write_task_run(
                    sid, jid, kind=kind, status='running', mode=mode,
                    trading_day=date or date_from, params=params,
                    started_at=job['started_at'])
                store.set_strategy_status(sid, 'running')
                # 停止按钮可能在本 job 跑起来之前就按下了（提交时带的
                # strategy_id 已能匹配），所以装载 spec 后再问一次
                runner = PaperRunner(
                    store, should_stop=lambda: bool(self._jobs[jid].get('cancel')))
                if mode == 'range':
                    results = runner.run_range(spec, date_from, date_to)
                    summary = f'区间补跑完成：{len(results)} 个交易日'
                    job['trading_day'] = date_from
                else:
                    day = date
                    if not day:
                        day = runner.ds.latest_bar_date(
                            spec.instruments, spec.benchmark)
                        if day is None:
                            raise RuntimeError('本地数据为空：请先更新数据快照')
                        print(f'未指定日期，使用数据最新交易日: {day}')
                    r = runner.run_one_day(spec, day)
                    job['trading_day'] = day
                    summary = (f'{day} 跑批完成：总资产 '
                               f'{r["portfolio_value"]:,.2f}，日收益 '
                               f'{r["today_return"]:.2%}，T+1 计划单 '
                               f'{r["planned_orders"]} 笔')
            job['result'] = summary
            job['status'] = 'ok'
        except RunStopped as e:
            # 用户中止：不是失败，不要在任务卡片上打红叉
            job['status'] = 'stopped'
            job['error'] = str(e)
            job['result'] = '已停止'
        except Exception as e:                      # 校验失败/数据缺失都走这里
            job['status'] = 'error'
            job['error'] = f'{type(e).__name__}: {e}'
        finally:
            job['log'] = buf.getvalue().splitlines()
            job['finished_at'] = self._now()
            if store is not None:
                try:
                    if sid:
                        store.write_task_run(
                            sid, jid, kind=kind, status=job['status'],
                            mode=mode,
                            trading_day=job.get('trading_day') or date or date_from,
                            params=params, summary=job.get('result') or '',
                            error=job.get('error') or '', log=job['log'],
                            started_at=job['started_at'],
                            finished_at=job['finished_at'])
                        store.set_strategy_status(sid, job['status'])
                except Exception as e:              # 落库失败也不能让线程炸掉
                    print(f'[WARN] 任务记录落库失败: {e}', flush=True)
                finally:
                    store.close()
            with self._lock:
                self._current = None


# ══════════════════════════════════════════════════════════════════
# HTTP 服务
# ══════════════════════════════════════════════════════════════════
class DashboardHandler(BaseHTTPRequestHandler):
    server_version = 'paper-dashboard/1.0'
    protocol_version = 'HTTP/1.1'

    # 由 make_server 注入
    db_path: str = DEFAULT_DB
    jobs: JobManager = None
    project_root: Path = _ROOT
    browse_root: Path = _ROOT             # 提交任务的目录浏览根（默认本项目目录）
    ide_url: str = ''                     # 可选：嵌入 code-server / JupyterLab
    editor: str = ''                      # 「编辑代码」的编辑器命令；'' = 自动（code → 系统默认）

    # ── 工具 ───────────────────────────────────────────────────
    def _send(self, body: bytes, status: int = 200,
              ctype: str = 'text/html; charset=utf-8') -> None:
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self._send(body, status, 'application/json; charset=utf-8')

    def _store(self) -> StateStore:
        return StateStore(self.db_path)

    # ── 列表页「操作」列 ───────────────────────────────────────
    def _source_file(self, source: str) -> Optional[Path]:
        """策略源文件在磁盘上的位置；找不到返回 None。

        优先取磁盘上真实存在的那份，因为提交时落盘的就是它；`config.source`
        可能是复制前的原路径，甚至是别的机器上的路径（早期在容器里注册的
        账户），不能直接拿去打开。
        """
        src = str(source or '').strip()
        if not src:
            return None
        p = Path(src)
        has_abs = p.is_absolute() or (len(src) > 1 and src[1] == ':')
        if has_abs:
            if p.is_file():
                return p
            # 原路径不在本机（换过机器/容器）：退回 strategies/ 下的同名文件
            same = self.project_root / 'strategies' / p.name
            return same if same.is_file() else None
        rel = self.project_root / src.replace('\\', '/').lstrip('/')
        return rel if rel.is_file() else None

    def _edit_href(self, sid: str, source: str) -> str:
        """「编辑代码」的目标：配了 IDE 就给 IDE 链接，否则给本地编辑器端点。

        两者都找不到源文件时返回 ''，前端把菜单项置灰。
        """
        p = self._source_file(source)
        if p is None:
            return ''
        if not self.ide_url:
            # 没配 IDE：转到 /api/open，由服务端调本机 VSCode / 默认程序打开
            return f'/api/open?sid={quote(str(sid))}'
        rel = self._rel_to_project(p) or p.name
        base = str(self.ide_url).strip()
        # 只有裸 host:port 才补 '/'（code-server / JupyterLab 的常规写法是
        # host:port/?folder=…）；带路径的（如 .../lab）原样保留
        if not urlsplit(base).path and '?' not in base:
            base += '/'
        sep = '&' if '?' in base else '?'
        params = []
        # --ide-url 里可能已经带了 folder（README 的示例就是），别再加一个
        if 'folder=' not in base:
            params.append(f'folder={quote(str(self.project_root))}')
        params.append(f'file={quote(rel)}')
        return f'{base}{sep}' + '&'.join(params)

    def _launch_editor(self, path: Path) -> str:
        """用本机编辑器打开文件，返回编辑器显示名。

        默认先找 VSCode 的 `code` 命令，找不到就交给系统默认程序
        （等同于双击文件）。`--editor` 可以指定别的编辑器命令。

        注意：只有看板跑在**浏览器所在的那台机器**上才有意义（默认绑
        127.0.0.1 就是这种情况）；用 --host 0.0.0.0 从别的机器访问时，
        打开的是服务端那台的编辑器。
        """
        editor = str(self.editor or '').strip()
        if editor:
            return self._spawn([editor, str(path)])
        code = shutil.which('code')
        if code:
            # VSCode 的 bin 下是 code.cmd / code.bat，CreateProcess 起不了
            # 批处理，得包一层 cmd /c
            if code.lower().endswith(('.cmd', '.bat')):
                return self._spawn(['cmd', '/c', code, str(path)])
            return self._spawn([code, str(path)])
        if os.name == 'nt':
            os.startfile(str(path))                  # noqa: S606
        elif sys.platform == 'darwin':
            self._spawn(['open', str(path)])
        else:
            self._spawn(['xdg-open', str(path)])
        return '系统默认程序'

    @staticmethod
    def _spawn(cmd: List[str]) -> str:
        """起一个脱离父进程的编辑器进程，返回命令名。"""
        kwargs: Dict[str, Any] = {}
        if os.name == 'nt':
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：看板重启不带走编辑器
            kwargs['creationflags'] = 0x00000008 | 0x00000200
        else:
            kwargs['start_new_session'] = True
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
        return Path(cmd[-2] if cmd[0] == 'cmd' else cmd[0]).name

    def _api_open(self, qs: Dict[str, List[str]]) -> None:
        """用本机编辑器打开策略源文件（没配 --ide-url 时的「编辑代码」）。

        只认状态库里已注册策略的 source —— 客户端传的是 strategy_id，
        不是路径，所以没有「打开任意本地文件」的口子。
        """
        sid = (qs.get('sid') or [''])[0]
        store = self._store()
        try:
            cfg = store.get_strategy(sid)
        finally:
            store.close()
        if cfg is None:
            self._json({'error': f'策略 {sid} 不存在'}, 404)
            return
        path = self._source_file(
            str((cfg.get('config') or {}).get('source') or ''))
        if path is None:
            self._json({'error': '找不到该策略的源文件（strategies/ 下没有，'
                                 'config.source 指向的路径也不存在）'}, 404)
            return
        try:
            editor = self._launch_editor(path)
        except Exception as e:
            self._json({'error': f'启动编辑器失败：{type(e).__name__}: {e}'}, 500)
            return
        self._json({'ok': True, 'editor': editor, 'path': str(path),
                    'message': f'已用 {editor} 打开 {path.name}'})

    def _rel_to_project(self, path: Path) -> Optional[str]:
        try:
            return path.resolve().relative_to(
                self.project_root.resolve()).as_posix()
        except (ValueError, OSError):
            return None

    def _delete(self, sid: str) -> Dict[str, Any]:
        """删除策略：状态库账户 + strategies/ 下的源文件与 meta.json。

        文件删除只在本项目 strategies/ 内进行，且只认「源文件名与策略文件
        同名」这一种情况 —— 不做任何按 strategy_id 的模糊匹配。
        """
        store = self._store()
        try:
            cfg = store.get_strategy(sid)
            if cfg is None:
                return {'error': f'策略 {sid} 不在状态库中'}
            cfg = dict(cfg)
            store.reset_strategy(sid)
        finally:
            store.close()

        src = str(cfg.get('config', {}).get('source') or '').strip()
        removed: List[str] = []
        if src:
            stem = Path(src).stem
            candidates = [self.project_root / 'strategies' / f'{stem}{suf}'
                          for suf in ('.py', '.ipynb')]
            candidates.append(self.project_root / 'strategies' /
                              f'{stem}.meta.json')
            for f in candidates:
                if f.is_file():
                    f.unlink()
                    removed.append(f.name)
        files_note = f'，同时删除文件 {", ".join(removed)}' if removed \
            else '（strategies/ 下未找到同名策略文件，仅清了状态库）'
        return {'ok': True, 'strategy_id': sid, 'removed': removed,
                'message': f'已删除策略「{sid}」{files_note}'}

    def _stop(self, sid: str) -> Dict[str, Any]:
        """停止部署任务：标记 stopped + 中止该策略正在跑的批次。

        看板跑批是单任务串行的，所以「正在跑」最多只有一个 job；
        若它正是这个策略，置取消标记，跑批会在下一个检查点抛 RunStopped
        （当天的账户状态不会被写坏，因为检查点都在天与天之间）。
        """
        store = self._store()
        try:
            if store.get_strategy(sid) is None:
                return {'error': f'策略 {sid} 不在状态库中'}
            store.set_strategy_status(sid, 'stopped')
        finally:
            store.close()
        stopped_job = self.jobs.cancel(sid)
        note = '，正在运行的批次已请求中止' if stopped_job else ''
        return {'ok': True, 'strategy_id': sid,
                'message': f'已停止策略「{sid}」的部署任务{note}'}

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f'[{self.log_date_time_string()}] {fmt % args}\n')

    # ── GET ────────────────────────────────────────────────────
    def do_GET(self) -> None:                        # noqa: N802
        u = urlparse(self.path)
        # 任务/策略名可能是非 ASCII（如 轮动宝v3），前端 encodeURIComponent 后
        # 路径是百分号编码，查库前必须解码，否则找不到任务直接 404。
        path, qs = unquote(u.path), parse_qs(u.query)
        try:
            if path == '/':
                self._index(qs)
            elif path == '/tasks':
                self._tasks(None, qs)
            elif path.startswith('/tasks/'):
                self._tasks(path[len('/tasks/'):], qs)
            elif path == '/ide':
                self._ide()
            elif path.startswith('/s/'):
                self._detail(path[3:])
            elif path.startswith('/api/series/'):
                self._series(path[len('/api/series/'):], qs.get('range', ['all'])[0])
            elif path.startswith('/api/job/'):
                self._job(path[len('/api/job/'):])
            elif path == '/api/tasks':
                self._api_tasks()
            elif path == '/api/browse':
                self._api_browse(qs)
            elif path == '/api/probe':
                self._api_probe(qs)
            elif path == '/api/open':
                self._api_open(qs)
            elif path == '/favicon.ico':
                self._send(b'', 204)
            else:
                self._send(b'<h1>404</h1>', 404)
        except BrokenPipeError:
            pass
        except Exception as e:                       # 页面报错也要看得见原因
            err = (f'<div class="msg bad">{h(type(e).__name__)}: {h(e)}</div>'
                   f'<p><a href="/">返回列表</a></p>')
            self._send(page('出错了', err), 500)

    def _index(self, qs: Dict[str, List[str]]) -> None:
        store = self._store()
        try:
            rows = webdata.list_strategies(store)
            last_runs = webdata.latest_run_times(store)
        finally:
            store.close()
        for r in rows:
            r['last_run'] = last_runs.get(r['strategy_id']) or {}
            r['edit_href'] = self._edit_href(r['strategy_id'], r['source'])
        files = webdata.discover_strategy_files(
            self.project_root, [r['strategy_id'] for r in rows])
        self._send(render_index(rows, files, self.db_path,
                                ide_url=self.ide_url,
                                msg=(qs.get('msg') or [''])[0]))

    def _detail(self, sid: str) -> None:
        store = self._store()
        try:
            d = webdata.strategy_detail(store, sid)
            series = webdata.equity_series(store, sid)
        finally:
            store.close()
        if d is None:
            self._send(page('未找到', f'<div class="msg bad">策略 {h(sid)} 不存在</div>'
                                      f'<p><a href="/">返回列表</a></p>'), 404)
            return
        self._send(render_detail(d, series, self.db_path, ide_url=self.ide_url))

    # ── 任务管理 ───────────────────────────────────────────────
    def _tasks(self, sid: Optional[str], qs: Dict[str, List[str]]) -> None:
        sid = (sid or '').strip() or None
        store = self._store()
        try:
            rows = webdata.task_list(store)
            detail = webdata.strategy_detail(store, sid) if sid else None
            runs = webdata.task_runs(store, sid, limit=30) if sid else []
        finally:
            store.close()
        if sid and detail is None:
            self._send(page('未找到', f'<div class="msg bad">任务 {h(sid)} 不存在</div>'
                                      f'<p><a href="/tasks">返回任务管理</a></p>'),
                       404)
            return
        self._send(render_tasks(rows, detail, runs, self.db_path,
                                ide_url=self.ide_url,
                                job_id=(qs.get('job') or [''])[0],
                                selected_id=sid or ''))

    def _ide(self) -> None:
        if not self.ide_url:
            self._send(page('编辑器', '<div class="msg bad">未配置 IDE 地址。</div>'
                            '<p>启动时加 <code>--ide-url http://127.0.0.1:8080/'
                            '?folder=/path</code>，即可在本页内嵌 code-server '
                            '或 JupyterLab。</p><p><a href="/">返回</a></p>'), 404)
            return
        body = (f'{nav("ide", self.ide_url)}'
                f'<header class="top"><h1>编辑器</h1>'
                f'<span class="crumb">{h(self.ide_url)}</span>'
                f'<span class="spacer"></span>'
                f'<a class="btn" href="{h(self.ide_url)}" target="_blank">'
                f'新窗口打开</a>{theme_button()}</header>'
                f'<iframe src="{h(self.ide_url)}" title="IDE" '
                f'style="width:100%;height:calc(100vh - 210px);'
                f'border:1px solid var(--border);border-radius:8px;'
                f'background:var(--surface)"></iframe>')
        self._send(page('编辑器 · 模拟盘看板', body))

    def _api_tasks(self) -> None:
        store = self._store()
        try:
            self._json(webdata.task_list(store))
        finally:
            store.close()

    def _api_browse(self, qs: Dict[str, List[str]]) -> None:
        self._json(submit.browse(self.browse_root,
                                 (qs.get('path') or [''])[0]))

    def _api_probe(self, qs: Dict[str, List[str]]) -> None:
        rel = (qs.get('path') or [''])[0]
        try:
            out = submit.probe(rel, browse_root=self.browse_root)
        except Exception as e:
            self._json({'error': f'{type(e).__name__}: {e}'}, 400)
            return
        self._json(out, 400 if out.get('error') else 200)

    def _series(self, sid: str, rng: str) -> None:
        store = self._store()
        try:
            data = webdata.equity_series(store, sid, rng)
        finally:
            store.close()
        self._json(data)

    def _job(self, jid: str) -> None:
        job = self.jobs.get(jid)
        if job is None:
            self._json({'error': 'job not found'}, 404)
            return
        self._json(job)

    # ── POST ───────────────────────────────────────────────────
    def do_POST(self) -> None:                       # noqa: N802
        path = urlparse(self.path).path
        if path not in ('/api/run', '/api/submit', '/api/stop', '/api/delete'):
            self._json({'error': 'not found'}, 404)
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            payload = json.loads(self.rfile.read(n) or b'{}')
        except (ValueError, TypeError):
            self._json({'error': '请求体不是合法 JSON'}, 400)
            return
        if path == '/api/submit':
            self._submit(payload)
            return
        if path in ('/api/stop', '/api/delete'):
            # strategy_id 里可能有非 ASCII；JSON 体本身是 UTF-8，无需解码
            sid = str(payload.get('strategy_id') or '').strip()
            if not sid:
                self._json({'error': '缺少 strategy_id'}, 400)
                return
            out = self._stop(sid) if path == '/api/stop' else self._delete(sid)
            self._json(out, 400 if out.get('error') else 200)
            return
        try:
            jid = self.jobs.submit(
                strategy_path=payload.get('strategy', ''),
                mode=payload.get('mode', 'auto'),
                date=payload.get('date') or None,
                date_from=payload.get('date_from') or None,
                date_to=payload.get('date_to') or None,
                strategy_id=payload.get('strategy_id', '') or '')
        except Exception as e:
            self._json({'error': f'{type(e).__name__}: {e}'}, 400)
            return
        self._json({'job_id': jid}, 202)

    def _submit(self, payload: Dict[str, Any]) -> None:
        """提交任务：落盘到 strategies/ → 建账户 → 立即跑一次。"""
        try:
            store = self._store()
            try:
                # store 一并传入：提交成功就把账户建好，前端立刻跳转的
                # /tasks/<id> 才不会因为「账户还没建」而 404
                res = submit.submit(
                    source_path=payload.get('source', ''),
                    params=payload,
                    strategies_dir=self.project_root / 'strategies',
                    browse_root=self.browse_root,
                    store=store)
            finally:
                store.close()
        except Exception as e:
            self._json({'error': f'{type(e).__name__}: {e}'}, 400)
            return
        params = {k: payload.get(k) or '' for k in
                  ('trigger_time', 'input_data', 'output_tag')}
        params.update(source=res['source_file'],
                      capital_base=res['capital_base'],
                      benchmark=res['benchmark'],
                      pool=res['pool'],
                      instrument_count=res['instrument_count'])
        try:
            jid = self.jobs.submit(strategy_path=res['path'], mode='auto',
                                   kind='submit', strategy_id=res['strategy_id'],
                                   params=params)
        except Exception as e:
            self._json({'error': f'任务已创建，但启动首次运行失败: {e}',
                        'strategy_id': res['strategy_id']}, 500)
            return
        self._json({'strategy_id': res['strategy_id'], 'job_id': jid,
                    'path': res['path'],
                    'instrument_count': res['instrument_count']}, 202)


def make_server(host: str, port: int, db_path: str, project_root: Path,
                browse_root=None, ide_url: str = '',
                editor: str = '') -> ThreadingHTTPServer:
    handler = type('Handler', (DashboardHandler,),
                   dict(db_path=db_path, jobs=JobManager(db_path),
                        project_root=Path(project_root),
                        browse_root=Path(browse_root or project_root),
                        ide_url=str(ide_url or ''),
                        editor=str(editor or '')))
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def main() -> int:
    ap = argparse.ArgumentParser(description='模拟盘可视化看板（本地 Web 服务）')
    ap.add_argument('--db', default=DEFAULT_DB, help=f'状态库（默认 {DEFAULT_DB}）')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--host', default='127.0.0.1',
                    help='监听地址（默认 127.0.0.1，仅本机；0.0.0.0 = 局域网可访问）')
    ap.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    ap.add_argument('--browse-root', default=None,
                    help='「提交模拟」文件浏览的根目录（默认项目目录 SmallQuant）')
    ap.add_argument('--ide-url', default='',
                    help='可选：页面内嵌的 IDE 地址（code-server / JupyterLab），'
                         '如 http://127.0.0.1:8080/?folder=D:/Data/bq')
    ap.add_argument('--editor', default='',
                    help='列表页「编辑代码」用的编辑器命令（默认自动：'
                         '先找 VSCode 的 code，找不到用系统默认程序）')
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f'状态库不存在: {args.db}\n请先跑一次 run_daily.py 生成账户。')
        return 1

    srv = make_server(args.host, args.port, args.db, _ROOT,
                      browse_root=args.browse_root, ide_url=args.ide_url,
                      editor=args.editor)
    url = f'http://{"127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host}:{args.port}/'
    print(f'模拟盘看板已启动: {url}')
    print(f'状态库: {args.db}')
    print('按 Ctrl+C 停止')
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
    finally:
        srv.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
