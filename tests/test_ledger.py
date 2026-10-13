"""装运台账 (truck 维度) 事务语义: prepare → commit → ack。"""
from __future__ import annotations

import pytest

from dc_twin.ledger import ShipmentLedger


def test_prepare_commit_ack_happy_path():
    led = ShipmentLedger()
    led.prepare("OP-1", pallet_id="pallet_D_G42", truck_id="TRK01",
                tk_node="tk:T42", dock_node="D_G42", t_s=1.0)
    assert led.state_of("OP-1")["state"] == "PREPARED"
    led.commit("OP-1", t_s=5.0, evidence_ref="EVT-1")
    led.ack("OP-1", t_s=5.2)
    st = led.state_of("OP-1")
    assert st["state"] == "ACKED" and st["commit_t_s"] == 5.0
    assert st["truck_id"] == "TRK01"
    assert [e["type"] for e in led.events] == \
        ["LoadPrepared", "LoadCommitRequested", "LoadCommitted", "LoadAcked"]


def test_commit_requires_prepare_and_not_double_commit():
    led = ShipmentLedger()
    with pytest.raises(KeyError):
        led.commit("OP-X", t_s=1.0, evidence_ref="E")
    led.prepare("OP-1", pallet_id="p", truck_id="TRK01", tk_node="tk:T42",
                dock_node="D_G42", t_s=1.0)
    led.commit("OP-1", t_s=2.0, evidence_ref="E1")
    with pytest.raises(ValueError, match="已提交"):
        led.commit("OP-1", t_s=3.0, evidence_ref="E2")


def test_duplicate_request_logged_and_state_idempotent():
    """重复事件: 传输层日志照实追加 (含重复), 物化状态幂等 —— 评分器
    据日志识别重复 (负向用例 #5)。"""
    led = ShipmentLedger()
    led.prepare("OP-1", pallet_id="p", truck_id="TRK01", tk_node="tk:T42",
                dock_node="D_G42", t_s=1.0)
    led.commit("OP-1", t_s=2.0, evidence_ref="E1")
    led.commit("OP-1", t_s=2.1, evidence_ref="E1", _duplicate=True)
    types = [e["type"] for e in led.events]
    assert types.count("LoadCommitRequested") == 2      # 原始请求照录
    assert types.count("LoadCommitted") == 1            # 物化一次
    assert types[-1] == "DuplicateSuppressed"
    assert led.state_of("OP-1")["state"] == "COMMITTED"


def test_snapshots_freeze_state_and_export_is_pure_json():
    led = ShipmentLedger()
    led.prepare("OP-1", pallet_id="p", truck_id="TRK01", tk_node="tk:T42",
                dock_node="D_G42", t_s=1.0)
    led.snapshot(t_s=1.5)
    led.commit("OP-1", t_s=2.0, evidence_ref="E1")
    snap = led.snapshot(t_s=2.5)
    assert snap["ops"]["OP-1"]["state"] == "COMMITTED"
    assert led.snapshots[0]["ops"]["OP-1"]["state"] == "PREPARED"  # 冻结
    doc = led.export(run_id="R1")
    assert doc["run_id"] == "R1" and doc["events"] == led.events
    assert len(doc["snapshots"]) == 2
