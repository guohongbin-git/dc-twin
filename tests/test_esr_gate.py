"""esr.gate — 场景进化门 (蓝图 §18.④/§18.3): 新注册类型必须附
(证据契约草案 + 消融子集 + 回归用例), 缺一拒绝; registry 对接 core
register_node_source / register_event_kind。校验器纯函数。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dc_twin.esr.crystallize import RegressionCase
from dc_twin.esr.gate import (
    AblationSubset,
    EvolutionProposal,
    EvidenceContractDraft,
    GateError,
    admit,
    review,
    validate_proposal,
)

_CORE = Path("~/1-Projects/desktop-agv-demo").expanduser().resolve()


def _reg_case(case_id: str = "REG-1") -> RegressionCase:
    return RegressionCase(
        case_id=case_id, scenario="m2_matrix.8task.determinism",
        asserts=({"metric": "op_errors", "op": "empty"},),
        covers_action="FIX-1", seeds=(7,),
        evidence="runs/run4_postfix.json")


_UNSET = object()


def _contract(**kw) -> EvidenceContractDraft:
    return EvidenceContractDraft(
        name="esr.contract.lane_lock/0.1",
        event_kinds=("LANE_INTERLOCK",),
        guarantees=("释放前提 = 联锁事件入账",),
        summary="lane-lock 释放证据化 (DetFix FIX-1)")


def _ablation() -> AblationSubset:
    return AblationSubset(
        name="ablation.INC-DETFIX-M2-001", base_subset="B0-B4",
        scenarios=("m2_matrix.8task.determinism",), seeds=(7,),
        metrics=("op_errors", "max_pen_m"))


def _proposal(kind="event_kind", *, contract=_UNSET, ablation=_UNSET,
              cases=_UNSET, payload=None) -> EvolutionProposal:
    return EvolutionProposal(
        proposal_id="PROP-DETFIX-001", kind=kind,
        payload=payload if payload is not None
        else {"kind": "LANE_INTERLOCK",
              "description": "行线道岔联锁 (活跃区 lane 释放证据)"},
        evidence_contract=_contract() if contract is _UNSET else contract,
        ablation=_ablation() if ablation is _UNSET else ablation,
        regression_cases=(_reg_case(),) if cases is _UNSET else cases)


# ------------------------------------------------------------- 纯校验器
def test_complete_proposal_is_approved():
    assert validate_proposal(_proposal()) == []


@pytest.mark.parametrize("drop", ["contract", "ablation", "cases"])
def test_missing_any_attachment_is_rejected(drop):
    """缺一拒绝: 契约草案 / 消融子集 / 回归用例。"""
    kw = {drop: None} if drop != "cases" else {"cases": ()}
    p = _proposal(**kw)
    errs = validate_proposal(p)
    assert errs and (p.proposal_id in " ".join(errs) or True)
    v = review(p)
    assert not v.approved and list(v.rejections) == errs


def test_ablation_subset_must_preregister_scenarios_seeds_metrics():
    p = _proposal(ablation=AblationSubset(
        name="abl", base_subset="B0-B4", scenarios=(), seeds=(),
        metrics=()))
    errs = " ".join(validate_proposal(p))
    assert all(k in errs for k in ("scenarios", "seeds", "metrics"))


def test_contract_must_be_draft_with_guarantees():
    p = _proposal(contract=EvidenceContractDraft(
        name="esr.contract.x/0.1", event_kinds=("LANE_INTERLOCK",),
        guarantees=(), status="ratified", summary=""))
    errs = " ".join(validate_proposal(p))
    assert "draft" in errs and "guarantees" in errs


def test_regression_case_without_asserts_rejected():
    bad = RegressionCase(case_id="REG-9", scenario="s", asserts=(),
                         covers_action="FIX-1")
    errs = " ".join(validate_proposal(_proposal(cases=(bad,))))
    assert "REG-9" in errs


def test_event_kind_payload_must_match_core_naming():
    p = _proposal(payload={"kind": "lane-lock!", "description": ""})
    errs = " ".join(validate_proposal(p))
    assert "LANE" in errs or "命名" in errs or "kind" in errs


def test_node_source_payload_requires_callable():
    p = _proposal(kind="node_source", payload={"fn": "not-a-fn"})
    assert validate_proposal(p)


def test_unknown_proposal_kind_rejected():
    p = _proposal(kind="leg_type")
    assert any("kind" in e for e in validate_proposal(p))


# ------------------------------------------------------------- 注册对接
def test_admit_refuses_registration_when_review_fails():
    calls: list = []
    p = _proposal(cases=())          # 缺回归用例 → 拒
    v = admit(p, event_kind_registrar=lambda k, d: calls.append(k) or k)
    assert not v.approved and not calls and list(v.rejections)


def test_admit_dispatches_to_event_kind_registrar():
    calls: list = []

    def fake_ek(kind, desc):
        calls.append((kind, desc))
        return kind

    v = admit(_proposal(), event_kind_registrar=fake_ek)
    assert v.approved and calls == [
        ("LANE_INTERLOCK", "行线道岔联锁 (活跃区 lane 释放证据)")]
    assert list(v.registered) == ["event_kind:LANE_INTERLOCK"]


def test_admit_dispatches_to_node_source_registrar():
    calls: list = []

    def fn(lay):
        return {}

    v = admit(_proposal(kind="node_source", payload={"fn": fn}),
              node_source_registrar=lambda f: calls.append(f))
    assert v.approved and calls == [fn]
    assert list(v.registered) == ["node_source:fn"]


def test_admit_real_core_integration():
    """真对接: core 注册表 (sys.path 注入, tests/conftest.py)。"""
    if str(_CORE) not in sys.path:
        sys.path.insert(0, str(_CORE))
    from services.world.evidence import EVENT_KINDS
    from sim.warehouse_cell import _NODE_SOURCES

    def gate_probe_nodes(lay):
        return {}

    v = admit(_proposal(kind="node_source", payload={"fn": gate_probe_nodes}))
    assert v.approved and gate_probe_nodes in _NODE_SOURCES

    v = admit(_proposal(payload={
        "kind": "GATE_PROBE_KIND",
        "description": "gate 集成探针 (测试)"}))
    assert v.approved and EVENT_KINDS["GATE_PROBE_KIND"] == \
        "gate 集成探针 (测试)"


def test_admit_duplicate_event_kind_raises_gate_error():
    if str(_CORE) not in sys.path:
        sys.path.insert(0, str(_CORE))
    from services.world.evidence import EVENT_KINDS
    if "GATE_PROBE_KIND2" in EVENT_KINDS:   # 会话重入防御
        del EVENT_KINDS["GATE_PROBE_KIND2"]
    p = _proposal(payload={"kind": "GATE_PROBE_KIND2",
                           "description": "dup 探针"})
    admit(p)
    with pytest.raises(GateError, match="GATE_PROBE_KIND2"):
        admit(p)


def test_admit_idempotent_allows_same_description_only():
    """闭环多轮: 同描述重注册幂等放行; 描述漂移仍拒绝。"""
    if str(_CORE) not in sys.path:
        sys.path.insert(0, str(_CORE))
    from services.world.evidence import EVENT_KINDS
    if "GATE_PROBE_KIND3" in EVENT_KINDS:
        del EVENT_KINDS["GATE_PROBE_KIND3"]
    p = _proposal(payload={"kind": "GATE_PROBE_KIND3",
                           "description": "idempotent 探针"})
    admit(p)
    v = admit(p, idempotent=True)
    assert v.approved
    assert list(v.registered) == ["event_kind:GATE_PROBE_KIND3@idempotent"]
    drifted = _proposal(payload={"kind": "GATE_PROBE_KIND3",
                                 "description": "换了描述"})
    with pytest.raises(GateError, match="描述漂移"):
        admit(drifted, idempotent=True)
