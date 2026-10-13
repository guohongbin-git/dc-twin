"""BerthTruck (mocap 静体 + 货箱 geom) 场景层契约 (蓝图 §3-P0)。"""
from __future__ import annotations

import mujoco
import pytest

from dc_twin.trucks import TruckCell, TruckSpec, berth_pose


def _cell(**kw):
    from sim.warehouse_cell import MatrixLayout

    lay = MatrixLayout(seed_pallets=2)
    return TruckCell(layout=lay, forklifts=("F01",), agvs=("A02",),
                     preload_stands=("D_G42",), seed_slots=[],
                     trucks=(TruckSpec("TRK01", "tk:T42"),), **kw)


def test_truck_is_mocap_with_cargo_geoms():
    cell = _cell()
    m = cell.model
    bid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "truck_TRK01")
    assert bid >= 0
    assert m.body_mocapid[bid] >= 0                     # mocap 静体
    names = {mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, g)
             for g in range(m.ngeom) if m.geom_bodyid[g] == bid}
    assert "truck_TRK01_bed_pp" in names               # 承托条带 (孔同 stand)
    assert "truck_TRK01_cab" in names
    assert any("cargo" in n for n in names)             # 货箱
    assert any("wheel" in n for n in names)


def test_berth_pose_matches_tk_node_geometry():
    from sim.warehouse_cell import MatrixLayout

    lay = MatrixLayout()
    bx, by, bz = berth_pose(lay, "tk:T42")
    tkx, tky, _ = tk_node(lay, "tk:T42")
    # 车体原点 = tk 节点 (条带孔中心 = 托盘交接点, 零偏置)
    assert (bx, by, bz) == (tkx, tky, 0.0)


def tk_node(lay, name):
    from dc_twin import layout as tk

    return tk.register()[name]


def test_pallet_transfer_point_clearance_over_bed():
    """承托面同 stand_top_z 链: 落托后凸台底 (0.066) 高于回缩销盘顶
    (0.062) 才能入孔落条带; 携托过泊位托盘底 (0.121) 高于条带顶;
    AGV 底盘顶 (0.062) 低于条带底 (0.093) —— 全程无碰撞走廊。"""
    from dc_twin.trucks import BED_TOP_Z

    assert BED_TOP_Z == 0.095
    assert 0.062 < BED_TOP_Z - 0.0025 < 0.095 + 0.001 < 0.121


def test_place_truck_moves_mocap():
    cell = _cell()
    midx = cell.trucks["TRK01"].mocap_idx
    y0 = float(cell.data.mocap_pos[midx][1])
    cell.place_truck("TRK01", (0.0, 3.0))
    y1 = float(cell.data.mocap_pos[midx][1])
    assert y1 == pytest.approx(3.0) and y1 != pytest.approx(y0)
