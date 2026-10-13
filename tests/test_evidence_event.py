"""装车证据事件 (四态之首) + 时间三元组契约 (C1, 蓝图 v0.5.1)。

- 运行时证据只带 t_capture/t_ingest/t_decide (+ 可选
  estimated_event_time + 不确定性标注); 真值 t_event 不进运行时证据。
- 同时钟域 (sim_t) 单调性: t_capture ≤ t_ingest ≤ t_decide (pytest)。
"""
from __future__ import annotations

import json

import pytest

from dc_twin.evidence import LoadEvent, build_load_event

_TRUTHY_KEYS = ("truth_event_time", "truth_settle_time", "truth_state",
                "durable_commit_time", "t_event")


def _frames():
    from services.world.evidence import EvidenceFrame

    return tuple(EvidenceFrame(t=5.0 + 0.05 * i, new_support=1.0,
                               old_support=0.0, pallet_x=-0.55,
                               pallet_y=2.26, pallet_z=0.081, speed=0.0)
                 for i in range(11))


def test_load_kind_registered_in_core():
    from services.world import evidence

    dc_twin_evidence = pytest.importorskip("dc_twin.evidence")
    dc_twin_evidence.register()
    assert "LOAD" in evidence.event_kinds()
    assert evidence.event_kinds()["LOAD"]


def test_event_triple_monotonic_same_clock():
    ev = build_load_event(
        event_id="EVT-1", operation_id="OP-1", pallet_id="pallet_D_G42",
        truck_id="TRK01", tk_node="tk:T42", dock_node="D_G42",
        task_id="T1", vehicle_id="A02", lease_id="tk:T42#1",
        frames=_frames(), t_ingest=6.02, t_decide=6.05,
        verdict_failures=())
    assert ev.clock_domain == "sim_t"
    assert 0.0 <= ev.t_capture <= ev.t_ingest <= ev.t_decide
    with pytest.raises(ValueError):
        build_load_event(
            event_id="EVT-2", operation_id="OP-1", pallet_id="p",
            truck_id="TRK01", tk_node="tk:T42", dock_node="D_G42",
            task_id="T1", vehicle_id="A02", lease_id="l", frames=_frames(),
            t_ingest=4.0, t_decide=6.0,          # < t_capture=5.5 → 拒绝
            verdict_failures=())


def test_runtime_event_carries_no_truth_time():
    ev = build_load_event(
        event_id="EVT-3", operation_id="OP-1", pallet_id="p",
        truck_id="TRK01", tk_node="tk:T42", dock_node="D_G42", task_id="T1",
        vehicle_id="A02", lease_id="l", frames=_frames(),
        t_ingest=6.0, t_decide=6.1, verdict_failures=(),
        estimated_event_time=(5.4, 0.2))         # 可选估计 + 不确定性
    d = json.loads(ev.to_json())
    leaked = set(d) & set(_TRUTHY_KEYS)
    assert not leaked, f"真值时间泄漏进运行时证据: {leaked}"
    assert d["estimated_event_time"] == [5.4, 0.2]   # 标注不确定性的估计值
    assert d["frames"][0].keys() >= {"t", "new_support"}


def test_event_roundtrip_and_verdict_summary():
    ev = build_load_event(
        event_id="EVT-4", operation_id="OP-2", pallet_id="p2",
        truck_id="TRK02", tk_node="tk:T43", dock_node="D_G43", task_id="T2",
        vehicle_id="A03", lease_id=None, frames=_frames(),
        t_ingest=6.0, t_decide=6.1, verdict_failures=("无有效租约",))
    ev2 = LoadEvent.from_json(ev.to_json())
    assert ev2 == ev
    assert not ev.passed and "无有效租约" in ev.failures


def test_stale_verdict_marked_expired():
    """C1 过期消息类: 裁决晚于 verdict TTL → 事件证据层标记过期。"""
    from services.world.evidence import VERDICT_TTL_S

    late = build_load_event(
        event_id="EVT-5", operation_id="OP-1", pallet_id="p",
        truck_id="TRK01", tk_node="tk:T42", dock_node="D_G42", task_id="T1",
        vehicle_id="A02", lease_id="l", frames=_frames(),
        t_ingest=5.5 + VERDICT_TTL_S, t_decide=5.5 + VERDICT_TTL_S + 0.1,
        verdict_failures=())
    assert not late.passed
    assert any("证据过期" in f for f in late.failures)
    # TTL 内正常裁决不受影响
    fresh = build_load_event(
        event_id="EVT-6", operation_id="OP-1", pallet_id="p",
        truck_id="TRK01", tk_node="tk:T42", dock_node="D_G42", task_id="T1",
        vehicle_id="A02", lease_id="l", frames=_frames(),
        t_ingest=5.6, t_decide=5.95,                        # TTL 内
        verdict_failures=())
    assert fresh.passed
