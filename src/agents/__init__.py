"""Agent 角色实现：每个角色一个模块（设计见 docs §4.2 角色清单）。

当前：
- producer —— 整站浅爬（爬取+AI清洗+即时向量化），多实例并行
预留：reviewer（质检）/ curator（术语整理）/ planner（学习规划）
"""
