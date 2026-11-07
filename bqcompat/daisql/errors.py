"""daiSQL 映射模块的异常类型。"""
from __future__ import annotations


class DialectError(RuntimeError):
    """SQL 里用了本地无法映射的平台方言函数。

    只在翻译阶段抛（执行前），消息里给出：函数名、为什么翻不了、可选的替代路径。
    绝不静默替换成近似值——策略盈亏对这类误差极敏感。
    """
