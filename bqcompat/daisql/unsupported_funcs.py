"""平台上有、本地确实无法等价映射的方言函数（只登记、不实现）。

原则：翻不了就**明确报错**，绝不静默替代成近似值——策略盈亏对这类误差极敏感。
每个条目都写清"为什么翻不了 + 建议怎么绕"。
"""
from __future__ import annotations

from .registry import unsupported

# 需要图中/行业分类数据，本地没有对应表
unsupported('c_indneutralize_resid',
            '需要行业分类做中性化回归，本地没有行业成分表（cn_stock_industry_component）')

# 平台侧的数据预处理/清洗节点，口径未公开
unsupported('c_preprocess',
            '平台侧数据预处理节点，口径未公开，无法保证等价')
unsupported('c_ffill',
            '平台侧前向填充语义未文档化；本地可用 last_value(x IGNORE NULLS) '
            'OVER (PARTITION BY instrument ORDER BY date) 自行实现')
unsupported('c_demean',
            '平台侧去均值口径未文档化；本地可用 x - avg(x) OVER (PARTITION BY date)')

# 依赖多列输入或参数集未公开
unsupported('m_ta_atr',
            'ATR 需要 high/low/close 与真实波幅，平台参数口径未文档化；'
            '本地可用 talib.ATR 在策略内自行计算')
unsupported('m_ta_boll',
            '布林带返回多列（上/中/下轨），平台单函数返回哪一列未文档化；'
            '本地可用 talib.BBANDS 在策略内计算')
unsupported('m_ta_ma',
            '与 m_avg 语义重叠但均线类型（SMA/WMA/EMA）未在函数名中体现；'
            '用 m_avg / m_ta_ema 明确指定')
unsupported('m_cov',
            '滚动协方差口径（样本/总体、对齐方式）未文档化；'
            '本地可用 m_corr 与 std 组合近似，或策略内自行计算')
