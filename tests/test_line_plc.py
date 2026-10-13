"""工位 PLC 状态字 (B4 _PlcStub 模式移植) 契约。"""
from __future__ import annotations

from dc_twin.line.plc import LinePlc


def test_issue_finish_ack_lifecycle():
    plc = LinePlc()
    plc.issue("OPL-001")
    assert plc.words["OPL-001"] == "BUSY"
    assert not plc.ack("OPL-001")
    plc.finish("OPL-001")
    assert plc.ack("OPL-001")


def test_finish_without_issue_is_noop():
    plc = LinePlc()
    plc.finish("OPL-X")
    assert plc.words.get("OPL-X") is None
    assert not plc.ack("OPL-X")


def test_poll_advances_clock_with_backoff():
    plc = LinePlc()
    clock = {"t": 0.0}

    def advance(s):
        clock["t"] += s
        if clock["t"] >= 0.20:          # 设备字迟至 0.2s 才置位
            plc.finish("OPL-1")

    plc.issue("OPL-1")
    assert plc.poll("OPL-1", advance)
    assert 0.19 < clock["t"] <= 0.60    # 有限重询 (3 次 × 退避)


def test_poll_gives_up_after_max_retries():
    plc = LinePlc()
    clock = {"t": 0.0}
    plc.issue("OPL-1")
    assert not plc.poll("OPL-1", lambda s: clock.__setitem__(
        "t", clock["t"] + s))
    assert clock["t"] <= 1.0            # 有界等待, 不挂死
