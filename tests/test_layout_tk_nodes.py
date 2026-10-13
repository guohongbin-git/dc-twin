"""tk:* 卡车位 (D_G 延伸段) 布局契约 (蓝图 §3-P0)。"""
from __future__ import annotations

import pytest

from dc_twin import layout as tk


def test_register_creates_tk_nodes_outside_dock_y():
    nodes = tk.register()
    from sim.warehouse_cell import MatrixLayout

    lay = MatrixLayout()
    assert nodes, "至少注册一个 tk 节点"
    for name, (x, y, z) in nodes.items():
        assert name.startswith("tk:")
        assert y == lay.dock_y + lay.p["berth_depth_y"] / 2   # dock_y 外侧半带深
        assert z == 0.0
        lo, hi = tk.BLIND_CORRIDOR_X
        assert not (lo < x < hi), f"{name} x={x} 落入盲走走廊"


def test_tk_nodes_pair_with_row4_docks_excluding_corridor():
    from sim.warehouse_cell import MatrixLayout

    lay = MatrixLayout()
    nodes = tk.register()
    lo, hi = tk.BLIND_CORRIDOR_X
    for c in (1, 2, 3, 4):
        cx, _ = lay.group_center(4, c)
        dock = f"D_G4{c}"
        if lo < cx < hi:               # 盲走走廊上的交付台不开卡车位
            assert f"tk:T4{c}" not in nodes
        else:
            assert nodes[f"tk:T4{c}"][0] == cx          # 同组心 (D_G 延伸段)


def test_dispatcher_resolves_tk_nodes():
    tk.register()
    from sim import dispatch

    d = dispatch.Dispatcher(fleet=dispatch.FLEET_STUB[:2])
    nodes = tk.register()
    first = sorted(nodes)[0]
    assert d.node_pos(first)[1] > 2.0    # 北泊位带外
    with pytest.raises(KeyError):
        d.node_pos("tk:NOPE")
