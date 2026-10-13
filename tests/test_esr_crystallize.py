"""esr.crystallize — 技能结晶四件套格式 (蓝图 §18.3): incident →
{根因假设, 修复动作, 回归用例, 证据契约修订}。

fixture: tests/fixtures/esr/detfix_m2 (DetFix 16 runs → 三修复,
docs/knowledge/detfix_fault_chain.md 取证移植), 不依赖真实运行。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from dc_twin.esr.crystallize import (
    IncidentRecord,
    crystallize,
    evaluate_assert,
    validate_semantics,
)

FIXTURE = Path(__file__).parent / "fixtures" / "esr" / "detfix_m2"


# ------------------------------------------------------------- 解析器
def test_crystallize_detfix_fixture_parses_four_pieces():
    rec = crystallize(FIXTURE)
    assert isinstance(rec, IncidentRecord)
    assert rec.incident_id == "INC-DETFIX-M2-001"
    # 四件套齐备:
    assert rec.root_cause.statement          # ① 根因假设
    assert len(rec.fix_actions) == 3         # ② 修复动作 (16 runs → 三修复)
    assert len(rec.regression_cases) == 3    # ③ 回归用例
    assert rec.contract.revision_kind == "amend"   # ④ 证据契约修订
    # 根因机制链还原 6 层故障链的关键环节:
    assert any("13mm" in m for m in rec.root_cause.mechanism)
    assert 0.0 < rec.root_cause.confidence <= 1.0


def test_crystallize_is_deterministic_and_roundtrips():
    a = crystallize(FIXTURE)
    b = crystallize(FIXTURE)
    assert a.to_dict() == b.to_dict()
    assert IncidentRecord.from_dict(a.to_dict()) == a


def test_crystallize_rejects_unknown_schema_version(tmp_path):
    bad = json.loads((FIXTURE / "incident.json").read_text())
    bad["schema"] = "esr.incident_record/9.9"
    d = tmp_path / "inc"
    d.mkdir()
    (d / "incident.json").write_text(json.dumps(bad, ensure_ascii=False))
    with pytest.raises(ValueError, match="schema"):
        crystallize(d)


def test_crystallize_reports_schema_violations_without_raising(tmp_path):
    """校验器返回错误清单 (纯函数), 供 LLM 草稿迭代修复。"""
    bad = json.loads((FIXTURE / "incident.json").read_text())
    del bad["fix_actions"]
    bad["root_cause_hypothesis"]["confidence"] = 1.5
    errors = validate_semantics(bad)
    assert any("fix_actions" in e for e in errors)
    assert any("confidence" in e for e in errors)


def test_semantics_require_regression_coverage_of_every_fix(tmp_path):
    """每条修复动作必须有回归保护 (缺一拒绝)。"""
    bad = json.loads((FIXTURE / "incident.json").read_text())
    bad["regression_cases"] = [c for c in bad["regression_cases"]
                               if c["covers_action"] != "FIX-2"]
    errors = validate_semantics(bad)
    assert any("FIX-2" in e for e in errors)


def test_semantics_reject_dangling_covers_action():
    bad = {"fix_actions": [{"action_id": "FIX-1"}],
           "regression_cases": [{"covers_action": "FIX-X"}],
           "evidence_contract_revision": {"revision_kind": "none",
                                          "status": "draft"}}
    errors = validate_semantics(bad)
    assert any("FIX-X" in e for e in errors)


def test_contract_revision_status_must_be_draft():
    bad = {"fix_actions": [{"action_id": "FIX-1"}],
           "regression_cases": [{"covers_action": "FIX-1"}],
           "evidence_contract_revision": {"revision_kind": "amend",
                                          "status": "ratified"}}
    assert any("draft" in e for e in validate_semantics(bad))


# ------------------------------------------------------------- 断言求值
def test_evaluate_assert_metric_lookup_and_ops():
    artifact = {"op_errors": [], "max_pen_m": 1.2e-5,
                "verdict": {"passed": True}, "kinds": ["PICK"]}
    assert evaluate_assert(artifact, {"metric": "op_errors", "op": "empty"})[0]
    assert evaluate_assert(
        artifact, {"metric": "max_pen_m", "op": "<=", "value": 1.6e-5})[0]
    assert evaluate_assert(
        artifact, {"metric": "max_pen_m", "op": ">", "value": 1e-9})[0]
    assert evaluate_assert(
        artifact, {"metric": "verdict.passed", "op": "eq", "value": True})[0]
    assert evaluate_assert(
        artifact, {"metric": "kinds", "op": "contains", "value": "PICK"})[0]
    ok, detail = evaluate_assert(
        artifact, {"metric": "op_errors", "op": "nonempty"})
    assert not ok and detail


def test_evaluate_assert_unknown_metric_or_op_is_failure_not_crash():
    ok, _ = evaluate_assert({}, {"metric": "nope", "op": "empty"})
    assert not ok
    ok, _ = evaluate_assert({"x": 1}, {"metric": "x", "op": "=~"})
    assert not ok
