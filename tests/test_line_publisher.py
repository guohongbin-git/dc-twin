"""产线状态发布器 (蓝图 §15): run_line 循环内周期写快照 JSON (面板
/api/line/state 文件契约数据源); 原子写 tmp+rename。"""
from __future__ import annotations

import json

import pytest

from dc_twin.line import publisher
from dc_twin.line.publisher import (DEFAULT_LINE_STATE_PATH,
                                    LineStatePublisher)
from dc_twin.line.runtime import run_line


class _FakeLive:
    """_LiveState 替身: 只需 snapshot() 纯读。"""

    def __init__(self, t: float = 1.0):
        self.t = t

    def snapshot(self) -> dict:
        return {"sim_t": self.t, "station": {"ST-PRESS": {"state": "idle"}}}


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_payload_contract(tmp_path):
    p = tmp_path / "line_state.json"
    pub = LineStatePublisher(p, _FakeLive(3.25), run_id="LINE-T",
                             interval_s=1.0)
    pub.publish()
    d = _read(p)
    assert d["available"] is True
    assert d["run_id"] == "LINE-T"
    assert d["core_version"]
    assert d["written_at"]                       # 墙钟 UTC ISO
    assert d["state"]["sim_t"] == 3.25
    assert "station" in d["state"]


def test_atomic_write_no_tmp_leftover(tmp_path):
    p = tmp_path / "line_state.json"
    pub = LineStatePublisher(p, _FakeLive(), run_id="LINE-T")
    for t in (1.0, 2.0):
        pub._live.t = t
        pub.publish()
        assert _read(p)["state"]["sim_t"] == t   # 每次均为完整 JSON
    assert not list(tmp_path.glob("*.tmp"))      # tmp+rename 无残留


def test_maybe_publish_interval_gating(tmp_path):
    p = tmp_path / "line_state.json"
    pub = LineStatePublisher(p, _FakeLive(), run_id="LINE-T", interval_s=5.0)
    assert pub.maybe_publish(0.0) is True        # 首拍必写
    assert pub.maybe_publish(2.0) is False       # 未到间隔
    assert pub.maybe_publish(4.9) is False
    assert pub.maybe_publish(5.0) is True        # 恰到间隔
    assert pub.writes == 2


def test_default_path_constant():
    assert str(DEFAULT_LINE_STATE_PATH) == \
        "~/1-Projects/dc-twin/reports/line_state.json"


def test_run_line_publishes_state_file(tmp_path):
    sp = tmp_path / "line_state.json"
    run = run_line(out_dir=tmp_path / "out", horizon_s=100.0, takt_s=45.0,
                   t0_s=12.0, line_pallets=8, run_id="LINE-PUB",
                   state_path=sp, state_interval_s=10.0)
    d = _read(sp)
    assert d["available"] is True
    assert d["run_id"] == "LINE-PUB"
    st = d["state"]
    assert st["sim_t"] == pytest.approx(run.state.snapshot()["sim_t"])
    for key in ("sim_t", "takt", "station", "carrier", "clearer",
                "evidence_recent", "production"):
        assert key in st, key
    assert set(st["takt"]["stock"]) == {"L-B1", "L-B2", "L-B3"}


def test_run_line_state_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DC_TWIN_LINE_STATE_PATH", str(tmp_path / "env.json"))
    run_line(out_dir=tmp_path / "out", horizon_s=100.0, takt_s=45.0,
             t0_s=12.0, line_pallets=8, run_id="LINE-ENV")
    assert _read(tmp_path / "env.json")["run_id"] == "LINE-ENV"


def test_run_line_state_publish_disable(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "DEFAULT_LINE_STATE_PATH",
                        tmp_path / "default.json")
    run_line(out_dir=tmp_path / "out", horizon_s=100.0, takt_s=45.0,
             t0_s=12.0, line_pallets=8, run_id="LINE-OFF", state_path=False)
    assert not (tmp_path / "default.json").exists()
