"""产线证据事件契约 (C1 时间三元组 t_capture/t_ingest/t_decide; 禁真值)。"""
from __future__ import annotations

import json

import pytest

from dc_twin.line import evidence as ev
from dc_twin.line.evidence import LineFrame


def _frames(t0=10.0, n=4):
    return tuple(LineFrame(t=t0 + 0.05 * i, clamp_on=True,
                           part_x=2.50, part_y=2.325, part_z=0.127,
                           speed=1e-4, head_z=0.17 - 0.01 * i,
                           force_n=8.0 + i) for i in range(n))


def test_event_kinds_registered():
    ev.register()
    from services.world.evidence import event_kinds

    ks = event_kinds()
    assert "FEED" in ks and "OP_COMPLETE" in ks


def test_feed_event_triple_and_json_roundtrip():
    e = ev.build_feed_event(
        event_id="E1", operation_id="OPL-001", pallet_id="pl_000",
        workpiece_id="wp_000", station_id="ST-PRESS", rack_node="L-B1",
        frames=_frames(), t_ingest=10.30, t_decide=10.40,
        verdict_failures=())
    assert e.passed and e.t_capture == pytest.approx(10.15)
    d = json.loads(e.to_json())
    e2 = ev.FeedEvent.from_json(e.to_json())
    assert e2 == e and d["kind"] == "FEED"
    assert "truth_event_time" not in d                    # 禁真值入证


def test_op_complete_event_carries_force_and_takt():
    e = ev.build_op_event(
        event_id="E2", operation_id="OPL-001", pallet_id="pl_000",
        workpiece_id="wp_000", station_id="ST-PRESS",
        frames=_frames(), t_ingest=10.30, t_decide=10.40,
        force_peak_n=18.2, force_mean_n=12.0, force_min_n=6.0,
        takt_s=40.0, t_start=9.0, t_end=30.0,
        clamp_confirmed=True, verdict_failures=())
    assert e.passed and e.force["peak_n"] == pytest.approx(18.2)
    assert e.takt["in_window"] is True
    assert e.takt["takt_s"] == pytest.approx(40.0)
    d = json.loads(e.to_json())
    assert d["kind"] == "OP_COMPLETE"
    assert ev.OpCompleteEvent.from_json(e.to_json()) == e


def test_triple_monotonic_enforced():
    with pytest.raises(ValueError):
        ev.build_feed_event(
            event_id="E", operation_id="O", pallet_id="p",
            workpiece_id="w", station_id="ST-PRESS", rack_node="L-B1",
            frames=_frames(), t_ingest=10.0,        # < t_capture 10.15
            t_decide=10.4, verdict_failures=())


def test_frame_time_order_enforced():
    with pytest.raises(ValueError):
        ev.build_feed_event(
            event_id="E", operation_id="O", pallet_id="p",
            workpiece_id="w", station_id="ST-PRESS", rack_node="L-B1",
            frames=(_frames()[2], _frames()[1]), t_ingest=11.0,
            t_decide=11.1, verdict_failures=())


def test_expired_evidence_flagged():
    frames = _frames(t0=10.0)                 # 末帧 10.15, TTL 0.5
    e = ev.build_feed_event(
        event_id="E", operation_id="O", pallet_id="p", workpiece_id="w",
        station_id="ST-PRESS", rack_node="L-B1", frames=frames,
        t_ingest=10.20, t_decide=10.15 + 0.5 + 0.01,       # 过期
        verdict_failures=())
    assert not e.passed
    assert any("过期" in f for f in e.failures)


def test_line_frame_jsonable():
    f = _frames(n=1)[0]
    d = f.to_dict()
    assert d["t"] == 10.0 and d["clamp_on"] is True and d["force_n"] == 8.0
