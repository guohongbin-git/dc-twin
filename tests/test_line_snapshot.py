"""runtime 状态快照契约 (面板 :8400 tab 产线 的数据源)。"""
from __future__ import annotations

import pytest

from dc_twin.line.runtime import latest_state, run_line


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("line_snap")
    return run_line(out_dir=out, horizon_s=100.0, takt_s=45.0, t0_s=12.0,
                    line_pallets=8, run_id="LINE-SNAP")


def test_snapshot_fields_stable(run):
    s = run.state.snapshot()
    for key in ("sim_t", "takt", "station", "carrier", "clearer",
                "evidence_recent", "production"):
        assert key in s, key
    st = s["station"]["ST-PRESS"]
    assert st["state"] in ("idle", "feeding", "pressing", "eject")
    assert st["plc_word"] in ("BUSY", "DONE", "-")
    assert s["carrier"]["vid"] == "A02"
    assert len(s["carrier"]["pos"]) == 2
    assert set(s["takt"]["stock"]) == {"L-B1", "L-B2", "L-B3"}
    for lvl in s["takt"]["stock"].values():
        assert set(lvl) == {"L0", "L1"}          # 2 层小架
    assert set(s["production"]) == {"physical_done", "ledger_committed",
                                    "mismatches"}


def test_snapshot_pure_read(run):
    s1 = run.state.snapshot()
    s2 = run.state.snapshot()
    assert s1["sim_t"] == s2["sim_t"]            # 无副作用 (不步进)
    assert s1["takt"] == s2["takt"]


def test_snapshot_scene_fields(run):
    s = run.state.snapshot()
    assert s["carrier"]["heading"] is not None
    st = s["station"]["ST-PRESS"]
    assert "slide_q" in st and "plunger_z" in st
    assert len(s["workpieces"]) >= 1
    w = s["workpieces"][0]
    assert set(w) == {"name", "x", "y", "z", "in_fixture"}
    assert len(s["pallets"]) >= 1
    assert set(s["pallets"][0]) == {"name", "x", "y"}
    ss = s["scene_static"]
    assert ss["format"] == "mjcf-mirror-v1"
    assert len(ss["geoms"]) > 50
    assert all({"id", "type", "size", "pos", "material"} <= set(g)
               for g in ss["geoms"])
    assert all(g["type"] in ("box", "cylinder", "capsule", "sphere",
                             "plane") for g in ss["geoms"])
    assert any(g.get("dyn") for g in ss["geoms"])   # 动态体标记
    assert any(g["material"] == "safety" for g in ss["geoms"])
    assert len(ss["dyn_geoms"]) >= 5                # 每帧 movers
    assert all({"id", "pos", "quat"} <= set(e) for e in ss["dyn_geoms"])
    # 便利 layout (AABB 投影)
    assert {"x", "y"} <= set(ss["layout"]["plane"])
    assert [r["node"] for r in ss["layout"]["racks"]] ==         ["L-B1", "L-B2", "L-B3"]
    stt = ss["layout"]["station"]
    assert stt["id"] == "ST-PRESS"
    assert len(stt["nest_rect"]) == 4 and len(stt["exit_rect"]) == 4
    assert stt["plunger"]["z_bottom"] < stt["plunger"]["z_rest"]
    assert len(ss["layout"]["staging"]) >= 1


def test_snapshot_pure_read_stable(run):
    a = run.state.snapshot(); b = run.state.snapshot()
    assert a == b


def test_snapshot_reflects_production(run):
    s = run.state.snapshot()
    assert s["production"]["physical_done"] >= 1
    assert s["station"]["ST-PRESS"]["last_event"] is not None
    assert len(s["evidence_recent"]) >= 1
    assert latest_state() is run.state
