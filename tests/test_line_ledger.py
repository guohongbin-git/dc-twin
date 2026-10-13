"""产线台账 (LineLedger) 契约 —— 段语义裁剪版 prepare→commit→ack。"""
from __future__ import annotations

import json

import pytest

from dc_twin.line.ledger import LineLedger


def _ops_of(doc):
    return {e["operation_id"] for e in doc["events"]
            if e["type"] == "OpCommitted" and not e.get("duplicate")}


def test_prepare_commit_ack_happy():
    led = LineLedger()
    led.record_delivery("pl_000", "L-B1", t_s=10.0)
    led.prepare_op("OPL-001", pallet_id="pl_000", workpiece_id="wp_000",
                   station_id="ST-PRESS", rack_node="L-B1", t_s=10.5)
    led.commit_op("OPL-001", t_s=12.0, evidence_ref="EVD-1", peak_n=18.2,
                  cycle_s=21.0, in_window=True)
    led.ack_op("OPL-001", t_s=12.3)
    st = led.state_of("OPL-001")
    assert st["state"] == "acked" and st["claims"]["in_window"] is True
    doc = led.export(run_id="R")
    assert _ops_of(doc) == {"OPL-001"}
    assert led.state_of("OPL-001")["commits"] == [12.0]


def test_duplicate_commit_recorded_and_suppressed():
    led = LineLedger()
    led.record_delivery("pl_000", "L-B1", t_s=10.0)
    led.prepare_op("OPL-001", pallet_id="pl_000", workpiece_id="wp_000",
                   station_id="ST-PRESS", rack_node="L-B1", t_s=10.5)
    led.commit_op("OPL-001", t_s=12.0, evidence_ref="EVD-1", peak_n=18.2,
                  cycle_s=21.0, in_window=True)
    led.commit_op("OPL-001", t_s=12.1, evidence_ref="EVD-1", peak_n=18.2,
                  cycle_s=21.0, in_window=True, _duplicate=True)
    doc = led.export(run_id="R")
    dups = [e for e in doc["events"]
            if e["type"] == "OpCommitted" and e.get("duplicate")]
    assert len(dups) == 1                                    # 照录
    assert led.state_of("OPL-001")["commits"] == [12.0]      # 物化抑制
    with pytest.raises(ValueError):
        led.commit_op("OPL-001", t_s=12.2, evidence_ref="E", peak_n=1.0,
                      cycle_s=1.0)                           # 非幂等三提交拒绝


def test_prepare_twice_rejected():
    led = LineLedger()
    led.record_delivery("pl_000", "L-B1", t_s=10.0)
    led.prepare_op("OPL-001", pallet_id="pl_000", workpiece_id="wp_000",
                   station_id="ST-PRESS", rack_node="L-B1", t_s=10.5)
    with pytest.raises(ValueError):
        led.prepare_op("OPL-001", pallet_id="pl_000",
                       workpiece_id="wp_000", station_id="ST-PRESS",
                       rack_node="L-B1", t_s=11.0)


def test_snapshot_and_replay_recovery():
    led = LineLedger()
    led.record_delivery("pl_000", "L-B1", t_s=10.0)
    led.prepare_op("OPL-001", pallet_id="pl_000", workpiece_id="wp_000",
                   station_id="ST-PRESS", rack_node="L-B1", t_s=10.5)
    led.commit_op("OPL-001", t_s=12.0, evidence_ref="EVD-1", peak_n=18.2,
                  cycle_s=21.0, in_window=True)
    snap = led.snapshot(t_s=12.5)
    assert snap["stock"]["L-B1"] == 0 and snap["wip"] is None
    assert snap["finished"] == 1
    assert snap["committed_ops"] == 1
    doc = led.export(run_id="R")
    json.loads(json.dumps(doc))                              # 可序列化
    led2 = LineLedger.from_export(doc)                       # 跨重启重放
    assert led2.state_of("OPL-001")["state"] == "committed"
    led2.ack_op("OPL-001", t_s=12.3)
    assert led2.state_of("OPL-001")["state"] == "acked"
    with pytest.raises(KeyError):
        led2.state_of("OPL-XXX")
