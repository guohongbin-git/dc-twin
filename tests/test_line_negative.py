"""产线六负向验收 (蓝图 §13): 评分器必须逐一识别。
空料上料 / 伪造工序完成 / 重复投料 / 未夹紧动作 / 节拍超时未报 /
错工位投料。"""
from __future__ import annotations

import pytest

from dc_twin.line.runtime import FAULTS, run_line
from dc_twin.scorer.line import (VERDICT_CONSISTENT, V_EMPTY, V_FORGED,
                                 V_TAKT, V_UNCLAMPED, V_WRONG,
                                 score_line_run)

HAPPY_VERDICT = {
    "empty_rack_feed": V_EMPTY,
    "forge_op": V_FORGED,
    "unclamped_press": V_UNCLAMPED,
    "takt_timeout_unreported": V_TAKT,
}


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    out = {}
    for fault in FAULTS:
        d = tmp_path_factory.mktemp(f"line_{fault}")
        out[fault] = run_line(faults=frozenset({fault}), out_dir=d,
                              horizon_s=150.0, takt_s=45.0, t0_s=12.0,
                              line_pallets=10, run_id=f"LINE-{fault}")
    return out


@pytest.mark.parametrize("fault,expected", sorted(HAPPY_VERDICT.items()))
def test_negative_verdict_on_faulted_operation(runs, fault, expected):
    """注入拍 (OPL-002) 判错, 其余拍一致。"""
    rep = score_line_run(runs[fault].out_dir)
    by_op = {o.operation_id: o for o in rep.ops}
    assert by_op["OPL-002"].verdict == expected, \
        (by_op["OPL-002"].verdict, by_op["OPL-002"].reason)
    for oid, o in by_op.items():
        if oid == "OPL-002":
            continue
        assert o.verdict == VERDICT_CONSISTENT, \
            (oid, o.verdict, o.reason)


def test_duplicate_feed_detected_and_deduped(runs):
    rep = score_line_run(runs["duplicate_feed"].out_dir)
    op = {o.operation_id: o for o in rep.ops}["OPL-002"]
    assert op.verdict == VERDICT_CONSISTENT          # 物化幂等
    assert "duplicate_feeds" in op.anomalies
    assert op.duplicate_count >= 1


def test_wrong_station_feed_flagged_on_delivery(runs):
    rep = score_line_run(runs["wrong_station_feed"].out_dir)
    wrong = [d for d in rep.deliveries if d.verdict == V_WRONG]
    assert wrong, [(d.pallet_id, d.verdict) for d in rep.deliveries]
    assert any("L-B3" in d.reason for d in wrong)
    # 交付错架 → 该拍停线 (无工序), 其余工位单一致
    assert rep.stoppage_ticks == [1]
    assert all(o.verdict == VERDICT_CONSISTENT for o in rep.ops)


def test_production_reconciliation_diverges_on_forge(runs):
    rep = score_line_run(runs["forge_op"].out_dir)
    assert not rep.reconciled
    assert rep.ledger_completions == rep.physical_completions + 1


def test_reconciliation_holds_when_consistent(runs):
    rep = score_line_run(runs["duplicate_feed"].out_dir)
    assert rep.reconciled and rep.zero_stoppage


def test_scorer_reads_only_exports(runs):
    """C2 Oracle 独立契约: 评分器不 import 被测系统业务模块。"""
    import dc_twin.scorer.line as oracle

    text = open(oracle.__file__, encoding="utf-8").read()
    for banned in ("dc_twin.runtime", "dc_twin.ledger", "dc_twin.trucks",
                   "dc_twin.line", "sim.", "services."):
        assert banned not in text, f"评分器耦合被测系统: {banned}"
    rep = score_line_run(runs["forge_op"].out_dir)
    assert rep.ops[0].verdict in (VERDICT_CONSISTENT, V_FORGED)
    assert rep.rule_version.startswith("dc_twin.")
