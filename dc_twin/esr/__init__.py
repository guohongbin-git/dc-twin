"""dc_twin.esr — ESR-Loop 自进化闭环 (蓝图 v0.5.1 §17-§18)。

SimReady 的规格由委员会发布后冻结; ESR 的规格由运行经验持续改写,
每一次进化都要过消融门 —— 进化不靠信仰, 靠消融 (§18)。

三件 (v0.1):
- crystallize: 技能结晶四件套格式 (incident → 根因假设/修复动作/
  回归用例/证据契约修订; §18.3, DetFix 即首个样本);
- loop: 五拍编排器 ①运行 → ②经验结晶 → ③知识沉淀 → ④场景进化 →
  ⑤消融验证, 每拍持久化阶段记录, 断点续跑; ②③ 可接 LLM (MCP 工具
  面接口预留, 不强依赖);
- gate: 场景进化门 —— 新注册类型必须附 (证据契约草案 + 消融子集 +
  回归用例), 缺一拒绝; registry 对接 core 既有注册表接口
  (register_node_source / register_event_kind)。
"""
from __future__ import annotations

__version__ = "0.1.0"
