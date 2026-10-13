"""tk:* 卡车位 —— 交付台 D_G 延伸段 (蓝图 §3-P0 第 1 条)。

- 位置: r=4 交付台 (北泊位带, y=dock_y) 同组心向外延伸, 落在
  [dock_y, dock_y+berth_depth_y] 泊位带外缘 (复用 berth_depth_y);
- 红线: 避 x∈(-2.22,-1.10) 盲走走廊 (W1 plan_agv_dock_lift 语义,
  走廊上不开卡车位);
- 进 core 方式: register_node_source 注册表 (不硬编码进
  warehouse_cell 主路径; 调度层 _layout_index 自动并入)。
"""
from __future__ import annotations

from sim.warehouse_cell import MatrixLayout, register_node_source

#: 盲走走廊开区间 (蓝图红线; LAYOUT.p["handoff_x"][0:2] 的语义北移复用)
BLIND_CORRIDOR_X = (-2.22, -1.10)

_REGISTERED = False


def berth_y(lay: MatrixLayout) -> float:
    """卡车位 y = dock_y 外侧半带深 (泊位带外缘)。"""
    return lay.dock_y + lay.p["berth_depth_y"] / 2


def berth_nodes(lay: MatrixLayout) -> dict[str, tuple[float, float, float]]:
    """r=4 各交付台的 tk:* 延伸段; 盲走走廊上的组不开位。"""
    lo, hi = BLIND_CORRIDOR_X
    out: dict[str, tuple[float, float, float]] = {}
    r = lay.p["group_rows"]
    for c in range(1, lay.p["group_cols"] + 1):
        cx, _cy = lay.group_center(r, c)
        if lo < cx < hi:
            continue                       # 盲走走廊: 不设卡车位
        out[f"tk:T{r}{c}"] = (cx, berth_y(lay), 0.0)
    return out


def register() -> dict[str, tuple[float, float, float]]:
    """注册进 core 节点源注册表 (幂等); 返回当前 tk 节点快照。"""
    global _REGISTERED
    if not _REGISTERED:
        register_node_source(berth_nodes)
        _REGISTERED = True
    return berth_nodes(MatrixLayout())
