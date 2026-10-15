"""T2M_HANDOFF_CELL 节点源 (text2mujoco→ESR 试点契约修订, ESR 门注册件)。

契约 (core register_node_source 同源): 纯函数 fn(MatrixLayout) ->
{node_id: (x, y, z)}; 节点全局唯一 (T2M: 前缀, 不占内核 H/D/货位名,
不与 tk:*/CONV-* 源冲突)。

三个调度锚点 (试点场景包 model.xml 局部坐标 → 布局坐标以 cell 锚平移):
- T2M:CELL        交接单元原点 (布位于 dock 泊位带外侧留一通道)
- T2M:BELT_END    取件位 (挡停面箱停点, 局部 -0.21, 0, 0.21)
- T2M:SORT_BIN    分拣箱内腔中心 (局部 +0.45, 0, 0.09)
"""
from __future__ import annotations

#: 场景包局部锚点 (与 reports/esr/pilot_t2m/scene/model.xml 一致)
CELL_LOCAL = {
    "T2M:BELT_END": (-0.210, 0.0, 0.210),
    "T2M:SORT_BIN": (0.450, 0.0, 0.090),
}


def t2m_handoff_cell_nodes(lay) -> dict[str, tuple[float, float, float]]:
    """T2M_HANDOFF_CELL 节点类派生规则 (纯函数, 同 layout 同结果)。"""
    # 单元布位: dock 泊位带外缘再让出一条 berth 深度, 避开卡车作业带
    cy = lay.dock_y + lay.p["berth_depth_y"] + 0.5
    cx = 0.0
    out: dict[str, tuple[float, float, float]] = {"T2M:CELL": (cx, cy, 0.0)}
    for name, (dx, dy, dz) in CELL_LOCAL.items():
        out[name] = (cx + dx, cy + dy, dz)
    return out
