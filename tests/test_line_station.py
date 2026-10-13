"""ST-PRESS 冲压机构 + LineCell 场景装配契约 (蓝图 §13: MuJoCo 机构
= 冲压头 slide actuator + 工件夹具; 力剖面物理)。"""
from __future__ import annotations

import mujoco
import pytest

from dc_twin.line.scene import LineCell, run_segments


def _cell(**kw):
    from sim.warehouse_cell import MatrixLayout

    lay = MatrixLayout(seed_pallets=0)
    lay.p["park"] = {**lay.p["park"], "A02": (2.10, 2.80, 0.0)}
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
    """冲压头 = position actuator, ctrlrange 必须显式
    (漏写 → 伺服静默失效)。"""
    m, T = cell.model, mujoco.mjtObj
    aid = mujoco.mj_name2id(m, T.mjOBJ_ACTUATOR, "line_ST-PRESS_press")
    assert aid >= 0
    assert m.actuator_ctrllimited[aid] == 1
    assert pytest.approx(-0.055) == m.actuator_ctrlrange[aid][0]
    assert pytest.approx(0.0) == m.actuator_ctrlrange[aid][1]


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
    assert L0_TOP_Z + 0.008 < float(wp[2]) < L0_TOP_Z + 0.018   # 坐薄顶
    # 预载托盘由架面条带承托 (承托率 ~1), 未穿透
    assert cell.max_penetration() < 2.5e-3


def test_nest_rect_for_scorer(cell):
    nr = cell.nest_rect("ST-PRESS")
    assert nr[0] < 2.50 < nr[1]                             # 托盘止点在矩形内
    assert nr[2] < 2.325 < nr[3]
    assert nr[1] - nr[0] > 0.060 and nr[3] - nr[2] > 0.060
    # 成品取出口 = nest 本位 (压制完成后原位取托)
    assert cell.exit_rect("ST-PRESS") == nr


def _seg(view, axis, target, *, speed=0.30, accel=0.20, settle=0.15,
         carry=False):
    if carry:
        speed, accel, settle = 0.15, 0.10, 0.30
    return (*view.seg(axis, target, speed, accel), settle)


def _nest_pl000(c):
    """测试工装: 顶升焊携托直投 nest 的末段 (拾取后北送落座), 与
    runtime 供料环同制式; pl_000 以预载位为拾取位。"""
    v = c.veh["A02"]
    run_segments(c, [_seg(v, "x", 2.31), _seg(v, "y", 1.94)])
    c.grip("A02", "pl_000", True)                    # 拾取位 = 槽西南 (0.19, 0.16)
    run_segments(c, [_seg(v, "y", 2.485, carry=True)])
    c.grip("A02", "pl_000", False)


def test_clamped_press_builds_force_profile(cell):
    """夹紧 → 冲压头下压 → 工件接触力 ≥ 门限 (力剖面物理)。"""
    _nest_pl000(cell)
    h = cell.presses["ST-PRESS"]
    cell.clamp("pl_000", True)
    assert cell.clamp_active("pl_000")
    from sim.controllers import Ramp

    run_segments(cell, [(h.press_aid,
                         lambda: Ramp(0.0, -0.087, 0.012, 0.02), 0.0, 0.30)])
    f = cell.contact_force_n("ST-PRESS", "wp_000")
    assert f >= h.spec.force_min_n, f"夹紧冲压力不足: {f:.2f} N"
    assert h.head_bottom_z(cell) < 0.092          # 头底压入坯顶


def test_empty_nest_press_has_no_workpiece_force(cell):
    """空 nest 冲压: 无工件接触力 (空料上料的物理判据)。"""
    c = _cell()
    c.warmup(0.5)
    from sim.controllers import Ramp

    h = c.presses["ST-PRESS"]
    run_segments(c, [(h.press_aid,
                      lambda: Ramp(0.0, -0.065, 0.008, 0.02), 0.0, 0.20)])
    assert c.contact_force_n("ST-PRESS", "wp_000") < 0.5


def test_staging_and_out_slots(cell):
    """暂存行自架面线 (2.10) 向南 0.30/槽, 北先浅取 (摆线自穿, 携托
    不跨未取位); 产出行 y=3.30 (A02 摆线北带之外)。"""
    x0, y0 = cell.staging_slot(0)
    assert (x0, y0) == (pytest.approx(2.50), pytest.approx(1.80))
    x1, y1 = cell.staging_slot(1)
    assert y0 - y1 == pytest.approx(0.30)          # 次取更南
    ox, oy = cell.out_slot(0)
    assert oy == pytest.approx(3.02) and ox > 2.4  # 产出行 (携托偏置落位)
