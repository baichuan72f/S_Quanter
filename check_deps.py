#!/usr/bin/env python3
"""check_deps.py —— 依赖体检：策略用到的第三方包，本地装了没有

用处：克隆策略跑出 `ModuleNotFoundError: No module named 'xxx'` 时，先跑这个，
它会列出**缺哪些、各被多少个文件用到、装哪一行命令**。

用法：
  .venv\\Scripts\\python.exe check_deps.py            # 全量报告
  .venv\\Scripts\\python.exe check_deps.py --missing   # 只看缺的（有缺口则非零退出）

扫描范围：strategies\\ 与「我的克隆策略\\」下所有 .py / .ipynb。
识别方式：AST 解析 import 语句（不会把 SQL 里的 `from cn_stock_bar1d` 误判为模块）。
"""
from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCAN_DIRS = [ROOT / 'strategies', ROOT / '我的克隆策略']

STDLIB = set(sys.stdlib_module_names) | {'__future__', 'typing_extensions'}
# 由 bqcompat 兼容层接管，不需要 pip 安装
COMPAT = {'bigquant', 'bigtrader', 'bigmodule', 'dai', 'bigcharts', 'bigalpha'}
# 工程内模块
LOCAL = {'bqcompat', 'paper', 'engine', 'smalltrader', 'data_source', 'queries',
         'check_deps', 'dashboard', 'run_daily', 'sim_replay'}
# 策略「自带」的辅助模块：写策略那台机器上的 .py，没有 PyPI 对应包。
# 实测依据：这些 import 都是 `from X.sub import ...` 形态，PyPI 上的同名包与之无关。
# （`community` 看似同类，其实是 python-louvain 提供的模块，已单独装）
KNOWN_LOCAL = {'agent', 'prompts', 'tools', 'config', 'model', 'benchmark_utils',
               'factor_analyse', 'unified_rebalancing_framework', '性能监控工具'}


def imports_of(src: str) -> set:
    out: set = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.add(node.module.split('.')[0])
    return out


def imports_in(path: Path) -> set:
    if path.suffix == '.py':
        return imports_of(path.read_text(encoding='utf-8', errors='replace'))
    try:
        nb = json.loads(path.read_text(encoding='utf-8', errors='replace'))
    except Exception:
        return set()
    mods: set = set()
    for cell in nb.get('cells', []):
        if cell.get('cell_type') == 'code':
            mods |= imports_of(''.join(cell.get('source') or []))
    return mods


def have(mod: str) -> bool:
    try:
        import importlib.util as iu
        return iu.find_spec(mod) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


_PYPI_CACHE: dict = {}


def on_pypi(mod: str) -> bool:
    """这个模块名在 PyPI 上有没有对应的包。

    用来区分两类「缺」：
      · PyPI 上有 → 能用 pip 装（真正可修）
      · PyPI 上没有 → 是策略自带的本地模块（如平台项目里的 agent.py / config.py），
        没法 pip 装，只能把那个文件一起拷过来或改写策略
    """
    if mod in _PYPI_CACHE:
        return _PYPI_CACHE[mod]
    ok = False
    try:
        import urllib.request
        url = f'https://pypi.org/simple/{mod}/'
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req, timeout=8) as r:
            ok = r.status == 200
    except Exception:  # noqa: BLE001 - 断网时保守当作「非 PyPI 包」，不误报可安装项
        ok = False
    _PYPI_CACHE[mod] = ok
    return ok


def main() -> int:
    only_missing = '--missing' in sys.argv
    counts: Counter = Counter()
    files = 0
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for p in d.rglob('*'):
            if p.suffix not in ('.py', '.ipynb') or not p.is_file():
                continue
            if '.graphflow' in str(p):
                continue
            files += 1
            for m in imports_in(p):
                counts[m] += 1

    # 本地脚本（同目录下的 .py 与 notebook 会互相 import），视为本地
    local_names = {p.stem for d in SCAN_DIRS if d.is_dir()
                   for p in list(d.rglob('*.py')) + list(d.rglob('*.ipynb'))}
    third = {m: c for m, c in counts.items()
             if m not in STDLIB and m not in LOCAL and m not in COMPAT
             and m not in local_names}
    missing = {m: c for m, c in third.items() if not have(m)}
    present = {m: c for m, c in third.items() if have(m)}

    # 缺的分两类：能从 PyPI 装的 vs 策略自带的本地模块（pip 装不了）
    missing_local = {m: c for m, c in missing.items()
                     if m in KNOWN_LOCAL or not on_pypi(m)}
    missing_pip = {m: c for m, c in missing.items() if m not in missing_local}

    print(f'解释器: {sys.executable}')
    print(f'扫描 {files} 个文件；第三方模块 {len(third)} 个：'
          f'已装 {len(present)}，缺 {len(missing)}'
          f'（可 pip 装 {len(missing_pip)}，策略自带 {len(missing_local)}）\n')

    if missing_pip:
        print('A. 缺且能从 PyPI 装（按被多少个文件使用排序）：')
        for m, c in sorted(missing_pip.items(), key=lambda kv: -kv[1]):
            print(f'  {c:5d} 个文件  {m}')
        print('\n一键补齐：')
        print('  .venv\\Scripts\\python.exe -m pip install -r requirements-extra.txt '
              '-c constraints.txt')
        print('  （或双击 install_deps.bat；只装单个：'
              '.venv\\Scripts\\python.exe -m pip install <包名>）')

    if missing_local:
        print('\nB. 缺但 PyPI 上没有——策略自带的本地模块（pip 装不了）：')
        for m, c in sorted(missing_local.items(), key=lambda kv: -kv[1]):
            print(f'  {c:5d} 个文件  {m}')
        print('  这些是写策略那台机器上的辅助文件（如 agent.py / config.py / tools.py），'
              '没随策略导出。')
        print('  处置：把同名 .py 一起拷进 strategies\\ 或改写策略，否则该策略跑不了。')

    if not missing:
        print('依赖齐全，没有缺的第三方模块。')

    if not only_missing:
        print('\n已装：' + (', '.join(f'{m}({c})' for m, c in
                                    sorted(present.items(), key=lambda kv: -kv[1]))
                          or '（无）'))
    # 只有「能 pip 装却缺」才算失败；策略自带模块缺失单独提示，不阻塞
    return 0 if not missing_pip else 1


if __name__ == '__main__':
    raise SystemExit(main())
