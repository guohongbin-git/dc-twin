#!/usr/bin/env python3
"""回归测试: ESR 技能结晶校验器 (validate_incident.py 薄封装)。

用仓库随包 fixture (tests/fixtures/esr/{detfix_m2,conveyor}) 验证通过
路径, 用合成样例逐条验证 schema 错误与四件套语义门的报错路径。

运行: python3 skills/esr-loop/scripts/test_validate_incident.py
(或 unittest discover)。零第三方依赖, 无 core 依赖。
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_incident as vi  # noqa: E402

# 仓库根: 优先从本文件位置向上找 (skill 装在仓库内), 兜底走校验器定位
_REPO = next((p for p in Path(__file__).resolve().parents
              if (p / "dc_twin" / "esr" / "crystallize.py").is_file()),
             None)
if _REPO is None:
    vi._locate_dc_twin()
    import dc_twin  # noqa: E402
    _REPO = Path(dc_twin.__file__).resolve().parent.parent
elif str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

REPO = _REPO
FIXTURES = REPO / "tests" / "fixtures" / "esr"

from validate_incident import SCHEMA_ID, load_incident, main, validate  # noqa: E402


def base_record() -> dict:
    """最小合法四件套样例 (结构对齐 DetFix/conveyor fixture)。"""
    return {
        "schema": SCHEMA_ID,
        "incident_id": "INC-TEST-1",
        "title": "样例事故",
        "window": {"t_start": 0.0, "t_end": 1.0,
                   "clock_domain": "sim_t"},
        "runs": ["run1"],
        "source": "单元测试样例",
        "root_cause_hypothesis": {
            "statement": "示例根因",
            "mechanism": ["环节一", "环节二"],
            "confidence": 0.9,
            "evidence_refs": [],
        },
        "fix_actions": [
            {"action_id": "FIX-1", "target": "示例对象",
             "description": "示例做法",
             "expected_effect": "示例效果"},
        ],
        "regression_cases": [
            {"case_id": "REG-1", "name": "示例回归", "seeds": [7],
             "scenario": "unit.sample",
             "asserts": [{"metric": "max_pen_m", "op": "<=",
                          "value": 1.6e-05}],
             "covers_action": "FIX-1",
             "evidence": "runs/run1.json"},
        ],
        "evidence_contract_revision": {
            "revision_kind": "amend",
            "summary": "示例修订",
            "proposed_changes": [
                {"path": "evidence_contract.sample",
                 "change": "新增示例事件类型",
                 "register": {"as": "event_kind",
                              "kind": "SAMPLE_EVENT",
                              "description": "示例事件"}},
            ],
            "status": "draft",
        },
    }


class ValidatorRegressionTests(unittest.TestCase):
    def assert_error(self, record: dict, fragment: str) -> None:
        errs = validate(record)
        self.assertTrue(any(fragment in e for e in errs),
                        f"期望错误含 {fragment!r}, 实得 {errs!r}")

    # ------------------------------------------------------------ 通过路径
    def test_detfix_fixture_passes(self) -> None:
        obj, _ = load_incident(FIXTURES / "detfix_m2")
        self.assertEqual(validate(obj), [])
        self.assertEqual(obj["incident_id"], "INC-DETFIX-M2-001")

    def test_conveyor_fixture_passes(self) -> None:
        obj, _ = load_incident(FIXTURES / "conveyor")
        self.assertEqual(validate(obj), [])
        self.assertEqual(obj["incident_id"], "INC-CONV-L1-001")

    def test_base_record_passes(self) -> None:
        self.assertEqual(validate(base_record()), [])

    def test_symbol_alias_ops_are_legal(self) -> None:
        rec = base_record()
        rec["regression_cases"][0]["asserts"] = [
            {"metric": "max_pen_m", "op": "<=", "value": 1.6e-05},
            {"metric": "cycles", "op": ">=", "value": 2},
        ]
        self.assertEqual(validate(rec), [])

    # ------------------------------------------------------------ schema 错误
    def test_missing_required_field(self) -> None:
        rec = base_record()
        del rec["title"]
        self.assert_error(rec, "缺必填字段 'title'")

    def test_bad_incident_id_pattern(self) -> None:
        rec = base_record()
        rec["incident_id"] = "no_prefix"
        self.assert_error(rec, "不匹配")

    def test_confidence_out_of_range(self) -> None:
        rec = base_record()
        rec["root_cause_hypothesis"]["confidence"] = 1.5
        self.assert_error(rec, "maximum")

    def test_extra_field_rejected(self) -> None:
        rec = base_record()
        rec["oops"] = 1
        self.assert_error(rec, "多余字段 'oops'")

    def test_wrong_schema_version(self) -> None:
        rec = base_record()
        rec["schema"] = "esr.incident_record/9.9"
        self.assert_error(rec, "版本不支持")

    # ------------------------------------------------------------ 语义门
    def test_fix_action_without_regression_guard(self) -> None:
        rec = base_record()
        rec["fix_actions"].append(
            {"action_id": "FIX-9", "target": "无保护对象",
             "description": "无回归用例"})
        self.assert_error(rec, "修复动作 'FIX-9' 无回归用例保护")

    def test_dangling_covers_action(self) -> None:
        rec = base_record()
        rec["regression_cases"][0]["covers_action"] = "NOPE"
        self.assert_error(rec, "covers_action 'NOPE' 不指向任何修复动作")

    def test_contract_status_must_be_draft(self) -> None:
        rec = base_record()
        rec["evidence_contract_revision"]["status"] = "final"
        self.assert_error(rec, "修订状态必须为 draft")

    def test_unknown_assert_op(self) -> None:
        rec = base_record()
        rec["regression_cases"][0]["asserts"] = [
            {"metric": "x", "op": "xor"}]
        self.assert_error(rec, "未知断言算子 'xor'")

    def test_value_op_requires_value(self) -> None:
        rec = base_record()
        rec["regression_cases"][0]["asserts"] = [
            {"metric": "max_pen_m", "op": "le"}]
        self.assert_error(rec, "算子 'le' 必须随附 value")

    def test_valueless_ops_legal(self) -> None:
        rec = base_record()
        rec["regression_cases"][0]["asserts"] = [
            {"metric": "op_errors", "op": "empty"}]
        self.assertEqual(validate(rec), [])

    # ------------------------------------------------------------ CLI
    def test_cli_fixture_json_ok(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([str(FIXTURES / "detfix_m2"), "--json"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["incident_id"], "INC-DETFIX-M2-001")

    def test_cli_failure_exit_code_and_report(self) -> None:
        rec = base_record()
        rec["fix_actions"].append(
            {"action_id": "FIX-9", "target": "无保护对象",
             "description": "无回归用例"})
        with tempfile.TemporaryDirectory() as tmp:
            inc = Path(tmp) / "incident.json"
            inc.write_text(json.dumps(rec, ensure_ascii=False),
                           encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([str(inc), "--json"])
            self.assertEqual(rc, 1)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["ok"])
            self.assertTrue(any("无回归用例保护" in e
                                for e in out["errors"]))

    def test_cli_missing_target_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                main([str(Path(tmp) / "absent")])
            self.assertEqual(ctx.exception.code, 2)

    def test_cli_directory_and_file_equivalent(self) -> None:
        rec = base_record()
        with tempfile.TemporaryDirectory() as tmp:
            inc = Path(tmp) / "incident.json"
            inc.write_text(json.dumps(rec, ensure_ascii=False),
                           encoding="utf-8")
            rc_dir = main([tmp, "--json"])
            rc_file = main([str(inc), "--json"])
            self.assertEqual((rc_dir, rc_file), (0, 0))


if __name__ == "__main__":
    unittest.main()
