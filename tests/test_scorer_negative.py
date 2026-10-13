"""六类负向验收 (C2): 评分器必须识别; 正确但未闭合事务判「待恢复」。"""
from __future__ import annotations

import pytest

from dc_twin.runtime import run_p0
from dc_twin.scorer.oracle import score_run

CASES = {
    "forge_commit": "MISMATCH_FORGED_SUCCESS",
    "wrong_truck": "MISMATCH_WRONG_TRUCK",
    "crash_before_commit": "PENDING_RECOVERY",
    "depart_unsettled": "MISMATCH_DEPARTURE_UNSETTLED",
}


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    out = {}
    for fault in (*CASES, "ack_lost", "duplicate_event"):
        d = tmp_path_factory.mktemp(f"p0_{fault}")
        out[fault] = run_p0(faults=frozenset({fault}), out_dir=d)
    return out


@pytest.mark.parametrize("fault,expected", sorted(CASES.items()))
def test_negative_verdict(runs, fault, expected):
    rep = score_run(runs[fault].out_dir)
    assert [o.verdict for o in rep.ops] == [expected], \
        [(o.verdict, o.reason) for o in rep.ops]


def test_ack_loss_flagged_but_commit_durable(runs):
    rep = score_run(runs["ack_lost"].out_dir)
    op = rep.ops[0]
    assert op.verdict == "CONSISTENT"                   # 持久提交是权威
    assert "ack_missing" in op.anomalies


def test_duplicate_events_detected_and_deduped(runs):
    rep = score_run(runs["duplicate_event"].out_dir)
    op = rep.ops[0]
    assert op.verdict == "CONSISTENT"
    assert "duplicate_events" in op.anomalies
    assert op.duplicate_count >= 1


def test_pending_recovery_is_not_complete_or_lost(runs):
    rep = score_run(runs["crash_before_commit"].out_dir)
    op = rep.ops[0]
    assert op.verdict == "PENDING_RECOVERY"
    assert op.verdict not in ("CONSISTENT", "MISMATCH_FORGED_SUCCESS")


def test_snapshot_boundary_distinguishes_transient_vs_persistent(runs):
    """同是「已装车未提交」: 卡车未离场 = 短暂未决 (待恢复);
    卡车已离场 = 持久账实错误 (未结清离场)。"""
    a = score_run(runs["crash_before_commit"].out_dir).ops[0]
    b = score_run(runs["depart_unsettled"].out_dir).ops[0]
    assert a.verdict == "PENDING_RECOVERY" and not a.truck_departed
    assert b.verdict == "MISMATCH_DEPARTURE_UNSETTLED" and b.truck_departed


def test_scorer_reads_only_exports(tmp_path, runs):
    """C2 Oracle 独立契约: 评分器不 import 被测系统业务模块。"""
    import dc_twin.scorer.oracle as oracle

    src = oracle.__file__
    text = open(src, encoding="utf-8").read()
    for banned in ("dc_twin.runtime", "dc_twin.ledger", "dc_twin.trucks",
                   "sim.", "services."):
        assert banned not in text, f"评分器耦合被测系统: {banned}"
    # 仅凭两份导出文件即可复算 (无运行时对象)
    rep = score_run(runs["forge_commit"].out_dir)
    assert rep.ops[0].verdict == "MISMATCH_FORGED_SUCCESS"
