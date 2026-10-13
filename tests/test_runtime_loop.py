"""P0 闭环运行时: 卡车到站 → 装车腿 → 证据事务 → 离场 → 导出。

验收口径 (蓝图 §3-P0): ≥1 卡车装车离场且账实三元等式成立;
非原子窗口上界可测 (max(t_commit − truth_settle) 入报告)。
"""
from __future__ import annotations

import json

import pytest

from dc_twin.runtime import run_p0


@pytest.fixture(scope="module")
def happy(tmp_path_factory):
    out = tmp_path_factory.mktemp("p0_happy")
    return run_p0(faults=frozenset(), out_dir=out)


def test_exports_written(happy):
    for name in ("truth.json", "ledger.json", "evidence_events.json"):
        assert (happy.out_dir / name).is_file()
        json.loads((happy.out_dir / name).read_text(encoding="utf-8"))


def test_one_truck_loaded_departed_and_closed(happy):
    assert happy.loaded_departed_closed >= 1
    assert happy.ops and happy.ops[0]["truth_state"] == "loaded"
    truth = json.loads((happy.out_dir / "truth.json").read_text())
    led = json.loads((happy.out_dir / "ledger.json").read_text())
    op = truth["ops"][0]
    trk = {t["truck_id"]: t for t in truth["trucks"]}[op["truck_id"]]
    assert trk["departed_t"] is not None                    # 离场
    ev = [e for e in led["events"] if e["type"] == "LoadCommitted"]
    assert ev and ev[0]["operation_id"] == op["operation_id"]
    # 账实三元等式: pallet_id + location (提交时在车斗矩形内) + custody
    # (同一卡车); 离场后货物随车到待命区 (staging)。
    assert op["pallet_id"].startswith("pallet_")
    x, y, _ = op["pallet_xyz_at_commit"]
    xmin, xmax, ymin, ymax = trk["bed_rect"]
    assert xmin <= x <= xmax and ymin <= y <= ymax
    fx, fy, _fz = op["final_pallet_xyz"]
    assert fy > ymax and trk["departed_t"] is not None      # 载托随车离场
    assert ev[0]["truck_id"] == op["truck_id"]


def test_nonatomic_window_measured_and_nonneg(happy):
    assert happy.nonatomic_window_s >= 0.0
    truth = json.loads((happy.out_dir / "truth.json").read_text())
    led = json.loads((happy.out_dir / "ledger.json").read_text())
    settle = truth["ops"][0]["truth_settle_time"]
    commit = next(e["ts_s"] for e in led["events"]
                  if e["type"] == "LoadCommitted")
    assert happy.nonatomic_window_s == pytest.approx(commit - settle, abs=1e-6)


def test_all_runtime_events_triple_monotonic(happy):
    """架构不变量: 全部运行时证据事件 t_capture ≤ t_ingest ≤ t_decide
    (同时钟域 sim_t; C1 验收)。"""
    evs = json.loads((happy.out_dir / "evidence_events.json").read_text())
    assert evs
    for e in evs:
        assert e["clock_domain"] == "sim_t"
        assert 0.0 <= e["t_capture"] <= e["t_ingest"] <= e["t_decide"]


def test_truth_export_has_no_runtime_evidence_leak(happy):
    truth = json.loads((happy.out_dir / "truth.json").read_text())
    # 真值通道只在独立导出: t_event 语义字段不进运行时证据文件
    evs = json.loads((happy.out_dir / "evidence_events.json").read_text())
    for e in evs:
        assert not ({"truth_event_time", "truth_settle_time",
                     "truth_state", "durable_commit_time"} & set(e))


def test_scorer_consistent_on_happy_run(happy):
    from dc_twin.scorer.oracle import score_run

    rep = score_run(happy.out_dir)
    assert [o.verdict for o in rep.ops] == ["CONSISTENT"]
    assert rep.loaded_departed_closed >= 1
    assert rep.run_id == happy.run_id
    assert rep.core_version
    assert rep.rule_version.startswith("dc_twin.oracle/")
    assert set(rep.input_hashes) == {"truth.json", "ledger.json"}
    assert all(len(v) == 64 for v in rep.input_hashes.values())   # sha256
