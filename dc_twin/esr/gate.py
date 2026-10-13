"""场景进化门 (蓝图 §18.④/§18.3): 新注册类型必须附 (证据契约草案 +
消融子集定义 + 回归用例), 缺一拒绝 —— 进化不靠信仰, 靠消融。

分层:
- validate_proposal / review: 纯函数校验器 (无 core 依赖, 错误清单
  可直接回喂 LLM 草稿回路);
- admit: 审查通过后经 core 既有注册表接口落册
  (register_node_source / register_event_kind, 懒导入), 注册器可
  注入 (测试/宿主替换); core 拒绝 (重名/非法名) → GateError。

对齐 §17.2 L+3: 消融子集的指标是预注册的 (metrics 进门时冻结),
回归用例沿用结晶格式 (esr.crystallize.RegressionCase)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .crystallize import ASSERT_OPS, RegressionCase

#: 事件类型命名规则 (与 core register_event_kind 同源, 提前纯校验)
_KIND_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class GateError(RuntimeError):
    """审查通过但注册表落册失败 (重名/非法名等)。"""


# ================================================================ 提案模型
@dataclass(frozen=True)
class EvidenceContractDraft:
    """证据契约草案 (§17.2 L+1): 哪些事件类型能证明哪些保证。"""

    name: str                       # e.g. "esr.contract.lane_lock/0.1"
    event_kinds: tuple[str, ...]    # 涉及的证据事件类型
    guarantees: tuple[str, ...]     # 契约保证 (提交语义/恢复行为/窗口上界)
    summary: str = ""
    status: str = "draft"           # 门只收草案; 定稿由 ⑤ 消融门推动


@dataclass(frozen=True)
class AblationSubset:
    """消融子集定义 (§17.2 L+3): B0-B4 基线上的子集 + 预注册指标。"""

    name: str
    base_subset: str                # 引用既有消融基线 (如 "B0-B4")
    scenarios: tuple[str, ...]
    seeds: tuple[int, ...]
    metrics: tuple[str, ...]        # 预注册指标 (进门冻结)


@dataclass(frozen=True)
class EvolutionProposal:
    """场景进化提案: 注册本体 + 三件随附 (缺一拒绝)。"""

    proposal_id: str
    kind: str                       # "event_kind" | "node_source"
    payload: dict                   # event_kind: {kind, description};
                                    # node_source: {fn, name?}
    evidence_contract: EvidenceContractDraft | None
    ablation: AblationSubset | None
    regression_cases: tuple[RegressionCase, ...] = ()


@dataclass(frozen=True)
class GateVerdict:
    """门裁决: 是否放行 + 拒绝理由清单 + 落册结果。"""

    proposal_id: str
    approved: bool
    rejections: tuple[str, ...] = ()
    registered: tuple[str, ...] = field(default_factory=tuple)


# ================================================================ 校验
def _nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def validate_proposal(p: EvolutionProposal) -> list[str]:
    """三件随附 + 载荷校验 (纯函数) → 错误清单 (空 = 放行)。"""
    errs: list[str] = []
    if not _nonempty_str(p.proposal_id):
        errs.append("proposal_id: 非空字符串必填")
    if p.kind not in ("event_kind", "node_source"):
        errs.append(f"kind: {p.kind!r} 非法 (event_kind | node_source)")

    # ① 证据契约草案 —— 缺一拒绝
    c = p.evidence_contract
    if not isinstance(c, EvidenceContractDraft):
        errs.append("evidence_contract: 缺证据契约草案 (缺一拒绝)")
    else:
        if not _nonempty_str(c.name):
            errs.append("evidence_contract.name: 非空必填")
        if not c.event_kinds:
            errs.append("evidence_contract.event_kinds: 空 (契约必须"
                        "声明哪些事件类型证明哪些状态)")
        if not c.guarantees:
            errs.append("evidence_contract.guarantees: 空 (契约保证"
                        "必填)")
        if c.status != "draft":
            errs.append(f"evidence_contract.status: {c.status!r} "
                        f"≠ draft (门只收草案)")

    # ② 消融子集定义 —— 缺一拒绝
    a = p.ablation
    if not isinstance(a, AblationSubset):
        errs.append("ablation: 缺消融子集定义 (缺一拒绝)")
    else:
        if not _nonempty_str(a.name):
            errs.append("ablation.name: 非空必填")
        if not _nonempty_str(a.base_subset):
            errs.append("ablation.base_subset: 空 (须引用既有消融基线)")
        if not a.scenarios:
            errs.append("ablation.scenarios: 空")
        if not a.seeds or not all(isinstance(s, int) and
                                  not isinstance(s, bool)
                                  for s in a.seeds):
            errs.append("ablation.seeds: 空/含非整数")
        if not a.metrics:
            errs.append("ablation.metrics: 空 (指标须预注册)")

    # ③ 回归用例 —— 缺一拒绝
    if not p.regression_cases:
        errs.append("regression_cases: 空 (新类型必须带回归保护, "
                    "缺一拒绝)")
    for case in p.regression_cases:
        if not isinstance(case, RegressionCase):
            errs.append(f"regression_cases: {case!r} 非 RegressionCase")
            continue
        if not _nonempty_str(case.case_id):
            errs.append("regression_cases: case_id 缺失")
        if not _nonempty_str(case.scenario):
            errs.append(f"regression_cases/{case.case_id}: scenario 空")
        if not case.asserts:
            errs.append(f"regression_cases/{case.case_id}: 无断言")
        for spec in case.asserts:
            if spec.get("op") not in ASSERT_OPS:
                errs.append(f"regression_cases/{case.case_id}: 未知断言"
                            f"算子 {spec.get('op')!r}")

    # 注册载荷形状 (core 命名规则前置纯校验, 落册时 core 仍终审)
    if p.kind == "event_kind":
        kind = p.payload.get("kind", "")
        if not _KIND_RE.match(kind or ""):
            errs.append(f"payload.kind: {kind!r} 不合 core 事件类型命名"
                        f" (大写字母/数字/下划线)")
    elif p.kind == "node_source":
        if not callable(p.payload.get("fn")):
            errs.append("payload.fn: node_source 载荷须为可调用纯函数")
    return errs


def review(p: EvolutionProposal) -> GateVerdict:
    """门审查 (不落册)。"""
    errs = validate_proposal(p)
    return GateVerdict(proposal_id=p.proposal_id, approved=not errs,
                       rejections=tuple(errs))


# ================================================================ 落册
def _core_registrars() -> tuple[Callable, Callable]:
    """core 既有注册表接口 (懒导入; 打包态装 agv-warehouse-core,
    工作仓 tests/conftest.py sys.path 注入)。"""
    from services.world.evidence import register_event_kind
    from sim.warehouse_cell import register_node_source
    return register_node_source, register_event_kind


def admit(p: EvolutionProposal, *,
          node_source_registrar: Callable | None = None,
          event_kind_registrar: Callable | None = None) -> GateVerdict:
    """审查 + 落册: 审查不过不注册; 通过后按 kind 分派 core 注册表。

    注册器可注入 (测试/宿主替换); 缺省用 core 真接口, core 拒绝
    (重名/非法) 抛 GateError。
    """
    verdict = review(p)
    if not verdict.approved:
        return verdict
    ns_reg, ek_reg = _core_registrars()
    ns_reg = node_source_registrar or ns_reg
    ek_reg = event_kind_registrar or ek_reg
    try:
        if p.kind == "event_kind":
            kind = ek_reg(p.payload["kind"],
                          p.payload.get("description", ""))
            return GateVerdict(p.proposal_id, True,
                               registered=(f"event_kind:{kind}",))
        fn = ns_reg(p.payload["fn"])
        name = p.payload.get("name") or getattr(fn, "__name__", "fn")
        return GateVerdict(p.proposal_id, True,
                           registered=(f"node_source:{name}",))
    except (ValueError, KeyError, TypeError) as exc:
        raise GateError(
            f"场景进化门落册失败 ({p.proposal_id}): {exc}") from exc
