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


def test_snapshot_reflects_production(run):
    s = run.state.snapshot()
    assert s["production"]["physical_done"] >= 1
    assert s["station"]["ST-PRESS"]["last_event"] is not None
    assert len(s["evidence_recent"]) >= 1
    assert latest_state() is run.state
