"""工位/线边库注册表 (蓝图 §13: 机构参数进可扩展注册表)。"""
from __future__ import annotations

import pytest

from dc_twin.line import registry as reg


def test_default_station_registered_with_mechanism_params():
    stations = reg.registered_stations()
    assert "ST-PRESS" in stations
    spec = reg.station("ST-PRESS")
    assert spec.station_id == "ST-PRESS"
    # 机构参数齐备: 节拍 / 冲压深度 / 力门限 / 到位容差
    assert spec.takt_s > 0
    assert spec.press_depth_m > 0
    assert spec.force_min_n > 0
    assert spec.feed_pos_tol_m > 0


def test_rack_bindings_cover_lb1_to_lb3():
    b = reg.rack_bindings()
    assert set(b) == {"L-B1", "L-B2", "L-B3"}
    assert b["L-B1"] == "ST-PRESS"          # ST-PRESS 主供料位


def test_register_station_and_duplicate_rejected():
    spec = reg.PressSpec(station_id="ST-T9000", takt_s=30.0,
                         press_depth_m=0.005, force_min_n=6.0,
                         feed_pos_tol_m=0.008)
    reg.register_station(spec)
    try:
        assert reg.station("ST-T9000") is spec
        with pytest.raises(ValueError):
            reg.register_station(spec)
    finally:
        reg.unregister_station("ST-T9000")
    with pytest.raises(KeyError):
        reg.station("ST-T9000")


def test_bind_rack_and_duplicate_rejected():
    reg.register_station(reg.PressSpec(station_id="ST-T9001", takt_s=30.0,
                                       press_depth_m=0.005, force_min_n=6.0,
                                       feed_pos_tol_m=0.008))
    try:
        reg.bind_rack("LX-9", "ST-T9001")
        assert reg.rack_bindings()["LX-9"] == "ST-T9001"
        with pytest.raises(ValueError):
            reg.bind_rack("LX-9", "ST-T9001")       # 重绑拒绝
        with pytest.raises(KeyError):
            reg.bind_rack("LX-8", "ST-NOPE")        # 未注册工位
    finally:
        reg.unregister_station("ST-T9001")


def test_station_sources_feeds():
    """工位供料源 = 绑定到该工位的架位 (追溯: rack→station)。"""
    assert reg.station_sources("ST-PRESS") == ("L-B1", "L-B2")
