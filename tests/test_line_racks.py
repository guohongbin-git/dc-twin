"""线边库 L-B1..B3 布局与几何契约 (蓝图 §13: 2 层小架, 注册进布局
可扩展点)。"""
from __future__ import annotations

import pytest

from dc_twin.line import racks


def test_lb_nodes_registered_via_node_source():
    """L-B1..B3 经 core register_node_source 注册, 可从布局解析。"""
    nodes = racks.register()
    from sim.warehouse_cell import MatrixLayout

    lay = MatrixLayout()
    assert set(nodes) == {"L-B1", "L-B2", "L-B3"}
    for name, (x, y, z) in nodes.items():
        assert y == pytest.approx(racks.LINE_CORRIDOR_Y)   # 产线走廊线上
        assert z == 0.0
        assert x > 2.2                                     # 矩阵东侧场外
    # 经 extra_nodes 求值进调度索引 (注册表契约)
    extra = lay.extra_nodes()
    for name in nodes:
        assert extra[name] == nodes[name]
    # 幂等
    assert racks.register() == nodes


def test_lb_nodes_spaced_clear_of_truck_berth():
    from sim.warehouse_cell import MatrixLayout

    lay = MatrixLayout()
    from dc_twin import layout as tk

    for x, _y, _z in tk.register().values():     # tk 卡车位在泊位带
        for name, (lx, ly, _lz) in racks.register().items():
            assert abs(lx - x) > 0.5 or abs(ly - _y) > 0.5, \
                f"{name} 与卡车泊位重叠"


def test_rack_xml_two_levels_and_agv_underpass():
    """2 层小架: L0 顶层 0.095 (AGV 交接面, 同 stand 链), L1 上层;
    净空链: AGV 顶 0.062 < L0 梁底; 立柱全部避开 AGV 停位点
    (对角立柱, 柱内面距 ≥0.15)。"""
    geo = racks.RackGeometry("L-B1", (2.50, racks.LINE_CORRIDOR_Y))
    xml = geo.xml()
    assert 'name="rack_L-B1_strip_pp"' in xml            # L0 承托条带
    assert 'name="rack_L-B1_upper_pp"' in xml            # L1 上层
    # L0 链: 台面顶 0.095, 梁底 > AGV 顶 0.062 (AGV 钻架下交接)
    assert racks.L0_TOP_Z == 0.095
    assert racks.L0_TOP_Z - 0.005 > 0.062
    # 立柱位置全表: 距 AGV 停位点 (架心) 最近 ≥0.15 (盘 r0.12+余量)
    for px, py in geo.leg_positions():
        d = ((px - geo.x) ** 2 + (py - geo.y) ** 2) ** 0.5
        assert d >= 0.15, f"立柱 ({px:.3f},{py:.3f}) 距架心 {d:.3f} 过近"


def test_rack_geometry_matches_registered_node():
    geo = racks.RackGeometry("L-B1", racks.register()["L-B1"][:2])
    assert geo.x == racks.register()["L-B1"][0]
    # 承托孔心 = 架心 (AGV 停位点 = 销盘交接点, 与 truck bed 同构)
    sx = [p for p in geo.strip_positions() if p[2] == racks.L0_TOP_Z]
    xs = sorted({round(p[0], 6) for p in sx})
    assert xs == [pytest.approx(geo.x - 0.033), pytest.approx(geo.x + 0.033)]
