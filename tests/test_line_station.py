"""ST-PRESS 冲压机构 + LineCell 场景装配契约 (蓝图 §13: MuJoCo 机构
= 冲压头 slide actuator + 工件夹具; 力剖面物理)。"""
from __future__ import annotations

import mujoco
import pytest

from dc_twin.line.scene import LineCell, run_segments


def _cell(**kw):
    from sim.warehouse_cell import MatrixLayout

    lay = MatrixLayout(seed_pallets=0)
    lay.p["park"] = {**lay.p["park"], "A02": (2.50, 2.10, 0.0)}
    kw.setdefault("line_pallets", 6)
    kw.setdefault("preload_racks", ("L-B1", "L-B2"))
    kw.setdefault("agvs", ("A02",))
    kw.setdefault("forklifts", ())
    kw.setdefault("seed_slots", [])
    return LineCell(layout=lay, **kw)


@pytest.fixture(scope="module")
def cell():
    c = _cell()
    c.warmup(0.5)
    return c


def test_press_mechanism_actuators_with_ctrlrange(cell):
    """冲压头/推料滑台 = position actuator, ctrlrange 必须显式
    (漏写 → 伺服静默失效)。"""
    m, T = cell.model, mujoco.mjtObj
    for act, lo, hi in (("line_ST-PRESS_press", -0.042, 0.0),
                        ("line_ST-PRESS_push", 0.0, 0.46)):
        aid = mujoco.mj_name2id(m, T.mjOBJ_ACTUATOR, act)
        assert aid >= 0, act
        assert m.actuator_ctrllimited[aid] == 1
        assert pytest.approx(lo) == m.actuator_ctrlrange[aid][0]
        assert pytest.approx(hi) == m.actuator_ctrlrange[aid][1]


def test_line_pallets_and_workpieces_seeded(cell):
    """预载托盘落 L-B1/L2 架面, 工件坐托盘顶; 产物链 pl_k ↔ wp_k。"""
    assert set(cell.pallets) >= {f"pl_{k:03d}" for k in range(6)}
    assert set(cell.workpieces) == {f"wp_{k:03d}" for k in range(6)}
    from dc_twin.line.racks import L0_TOP_Z

    for k, rack in ((0, "L-B1"), (1, "L-B2")):
        pos = cell.pallet_pose(f"pl_{k:03d}")[0]
        nx, ny = cell.racks[rack].xy
        assert abs(float(pos[0]) - nx) < 5e-3 and abs(float(pos[1]) - ny) < 5e-3
    wp = cell.wp_pose("wp_000")[0]
    assert L0_TOP_Z + 0.030 < float(wp[2]) < L0_TOP_Z + 0.040   # 坐顶面
    # 预载托盘由架面条带承托 (承托率 ~1), 未穿透
    assert cell.max_penetration() < 1e-3


def test_nest_and_exit_rects_for_scorer(cell):
    nr = cell.nest_rect("ST-PRESS")
    er = cell.exit_rect("ST-PRESS")
    assert nr[0] < 2.50 < nr[1]                             # 托盘止点在矩形内
    assert nr[2] < 2.325 < nr[3] and er[2] < 2.525 < er[3]
    assert er[2] > nr[3]                                    # 出口在北
    # 矩形覆盖托盘满截面 (半长 0.030 + 容差)
    assert nr[1] - nr[0] > 0.060 and nr[3] - nr[2] > 0.060


def test_feed_push_stroke_places_pallet_in_nest(cell):
    """推料滑台把托盘+工件从架面推入夹具 nest (物理滑动, 背止挡定
    位)。"""
    c = _cell()
    c.warmup(0.5)
    h = c.presses["ST-PRESS"]
    seg = (h.push_aid, lambda: __import__("sim.controllers", fromlist=["Ramp"])
           .Ramp(0.0, 0.235, 0.05, 0.10), 0.0, 0.10)
    run_segments(c, [seg])
    pos = c.pallet_pose("pl_000")[0]
    ny = c.nest_rect("ST-PRESS")
    assert ny[2] <= float(pos[1]) <= ny[3]                      # 入 nest
    assert abs(float(pos[0]) - h.geo.x) < 5e-3
    wp = c.wp_pose("wp_000")[0]                                 # 工件随行
    assert ny[0] <= float(wp[0]) <= ny[1] and ny[2] <= float(wp[1]) <= ny[3]


def test_clamped_press_builds_force_profile(cell):
    """夹紧 → 冲压头下压 → 工件接触力 ≥ 门限 (力剖面物理)。"""
    c = _cell()
    c.warmup(0.5)
    from sim.controllers import Ramp

    h = c.presses["ST-PRESS"]
    run_segments(c, [(h.push_aid, lambda: Ramp(0.0, 0.235, 0.05, 0.10),
                      0.0, 0.10)])
    c.clamp("pl_000", True)
    assert c.clamp_active("pl_000")
    run_segments(c, [(h.press_aid,
                      lambda: Ramp(0.0, -0.035, 0.008, 0.02), 0.0, 0.20)])
    f = c.contact_force_n("ST-PRESS", "wp_000")
    assert f >= h.spec.force_min_n, f"夹紧冲压力不足: {f:.2f} N"
    # 成形深度: 头底面压入工件顶面以下
    assert h.head_bottom_z(c) < 0.141


def test_unclamped_press_has_no_clamp_confirmation(cell):
    c = _cell()
    c.warmup(0.5)
    from sim.controllers import Ramp

    h = c.presses["ST-PRESS"]
    run_segments(c, [(h.push_aid, lambda: Ramp(0.0, 0.235, 0.05, 0.10),
                      0.0, 0.10)])
    assert not c.clamp_active("pl_000")
    run_segments(c, [(h.press_aid,
                      lambda: Ramp(0.0, -0.035, 0.008, 0.02), 0.0, 0.20)])
    # 未夹紧: 夹紧确认缺失 (门控输入; 力可能仍建立 —— 由确认位判定)
    assert not c.clamp_active("pl_000")
    assert c.contact_force_n("ST-PRESS", "wp_000") >= 0.0


def test_empty_nest_press_has_no_workpiece_force(cell):
    """空 nest 冲压: 无工件接触力 (空料上料的物理判据)。"""
    c = _cell()
    c.warmup(0.5)
    from sim.controllers import Ramp

    h = c.presses["ST-PRESS"]
    run_segments(c, [(h.press_aid,
                      lambda: Ramp(0.0, -0.035, 0.008, 0.02), 0.0, 0.20)])
    assert c.contact_force_n("ST-PRESS", "wp_000") < 0.5


def test_staging_and_out_slots(cell):
    """暂存/产出位: 首件位与走廊同列, 槽距 0.10。"""
    x0, y0 = c_staging = cell.staging_slot(0)
    assert y0 == pytest.approx(2.10) and x0 > 3.8
    x1, y1 = cell.staging_slot(1)
    assert y1 - y0 == pytest.approx(0.10)
    ox, oy = cell.out_slot(0)
    assert ox == pytest.approx(2.50) and oy < 2.10
