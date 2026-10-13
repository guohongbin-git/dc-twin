"""产线供料闭环 runtime (run_line) 验收口径 (蓝图 §13):
takt 拉料连续运转 / 零停线 / 账实对账一致 / 证据事件齐全。"""
from __future__ import annotations

import json

import pytest

from dc_twin.line.runtime import run_line


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("line_happy")
    return run_line(faults=frozenset(), out_dir=out, horizon_s=150.0,
                    takt_s=45.0, t0_s=12.0, line_pallets=10,
                    run_id="LINE-HAPPY")


def test_exports_written(run):
    for name in ("truth.json", "line_ledger.json", "evidence_events.json"):
        assert (run.out_dir / name).is_file()
        json.loads((run.out_dir / name).read_text(encoding="utf-8"))


def test_takt_pull_runs_continuously_no_stoppage(run):
    truth = run.truth
    assert len(truth["cycles"]) >= 2                 # ≥2 拍物理工序
    assert truth["stoppage_ticks"] == [], truth["stock_timeline"]
    for s in truth["stock_timeline"]:                # 零停线: 消耗时在架 ≥1
        assert s["stock"]["L-B1"] >= 1, s


def test_ledger_matches_physical_completions(run):
    truth = run.truth
    led = run.ledger_doc
    committed = {e["operation_id"] for e in led["events"]
                 if e["type"] == "OpCommitted" and not e.get("duplicate")}
    assert committed == {c["op_id"] for c in truth["cycles"]}
    assert len(committed) == len(truth["cycles"]) >= 2


def test_evidence_events_complete_and_monotonic(run):
    evs = json.loads(
        (run.out_dir / "evidence_events.json").read_text(encoding="utf-8"))
    kinds = [e["kind"] for e in evs]
    assert kinds.count("FEED") >= 2 and kinds.count("OP_COMPLETE") >= 2
    for e in evs:
        assert e["clock_domain"] == "sim_t"
        assert 0 <= e["t_capture"] <= e["t_ingest"] <= e["t_decide"]
        assert "truth_event_time" not in e           # 禁真值入证
    ops = [e for e in evs if e["kind"] == "OP_COMPLETE"]
    assert all(e["verdict"]["passed"] for e in ops)
    assert all(e["force"]["peak_n"] >= e["force"]["min_required_n"]
               for e in ops)
    assert all(e["takt"]["in_window"] for e in ops)


def test_material_traceability_pallet_to_operation(run):
    """物料追溯: pallet_id → 工位 operation_id (同架位/同工位链)。"""
    truth = run.truth
    led = run.ledger_doc
    delivery = {e["pallet_id"]: e for e in led["events"]
                if e["type"] == "DeliveryRecorded"}
    for c in truth["cycles"]:
        d = delivery[c["pallet_id"]]
        assert d["rack_node"] == c["rack_node"] == "L-B1"
        assert d["station_bound"] == "ST-PRESS"
    for e in led["events"]:
        if e["type"] == "OpCommitted":
            assert e["pallet_id"] in delivery


def test_finished_pallets_reach_outbound(run):
    truth = run.truth
    assert len(truth["finished"]) >= 1                # 成品托盘出工位
    for f in truth["finished"]:
        assert f["out_slot"][1] > 2.9                 # 产出行 y=3.02 (出门架)


def test_physics_clean_no_penetration(run):
    assert run.audit["max_pen_m"] < 2.5e-3, run.audit["max_pen_m"]


def test_scorer_reconciles_happy_run(run):
    from dc_twin.scorer.line import score_line_run

    rep = score_line_run(run.out_dir)
    assert all(o.verdict == "CONSISTENT" for o in rep.ops), \
        [(o.operation_id, o.verdict, o.reason) for o in rep.ops]
    assert rep.reconciled and rep.zero_stoppage
