"""BerthTruck —— 月台卡车 (mocap 静体 + 货箱 geom; 蓝图 §3-P0)。

场景层 MJCF 增量: 在 WarehouseCell 生成的 XML 上注入卡车 mocap 体。
承托面复用台位条带孔几何 (凸台垂入孔内, 与 H/D 台同一物理链, 插叉/
顶销走廊已验证): 条带顶 0.080, AGV 底盘顶 0.062 < 条带底 0.075 <
销盘升顶 0.092 < 携托底面 —— 进出泊位无碰撞。

卡车经 mocap 驱动到站/离场 (时刻表), 载托后摩擦随动离场。
"""
from __future__ import annotations

from dataclasses import dataclass

import mujoco

from sim.warehouse_cell import MatrixLayout, WarehouseCell

#: 承托条带顶面 z —— 必须同 stand_top_z (0.095): 落托后凸台底
#: (top+0.001-0.030=0.066) 高于回缩销盘顶 (0.062), 才能落进条带孔
#: 而不骑在 AGV 圆盘上; 携托过泊位托盘底 (0.121) 高于条带顶 26mm。
BED_TOP_Z = 0.095
#: 车斗矩形半宽 (x, y) —— 评分器 custody 位置核对 (truth.json bed_rect)
BED_HALF_XY = (0.09, 0.075)
#: 到站/离场待命 y (泊位北侧, 场外)
STAGING_Y = 3.10


@dataclass(frozen=True)
class TruckSpec:
    truck_id: str
    tk_node: str


def berth_pose(lay: MatrixLayout, tk_node: str) -> tuple[float, float, float]:
    """车体原点 = tk 节点 (条带孔中心 = 托盘交接点)。"""
    from dc_twin import layout as tk

    nodes = tk.register()
    if tk_node not in nodes:
        raise KeyError(f"未知卡车位 {tk_node!r}; 可用 {sorted(nodes)}")
    return nodes[tk_node]


def _truck_body_xml(spec: TruckSpec, x: float, y: float) -> str:
    """mocap 卡车: 4 承托条带 (孔) + 横梁 + 驾驶室 + 货箱墙/顶 + 车轮。

    进泊位走廊 (y ∈ [dock_y, tk_y] × x ±0.12, z < 0.095) 内只允许
    条带 (z ≥ 0.075) —— AGV 底盘 (顶 0.062) 与销盘通道净空。
    """
    tid = spec.truck_id
    top = BED_TOP_Z
    return f"""
    <body name="truck_{tid}" mocap="true" pos="{x:.6f} {y:.6f} 0">
      <geom name="truck_{tid}_bed_pp" type="box" pos="0.033 0.047 {top - 0.002:.4f}" size="0.011 0.023 0.002" rgba="0.30 0.32 0.36 1"/>
      <geom name="truck_{tid}_bed_pn" type="box" pos="0.033 -0.047 {top - 0.002:.4f}" size="0.011 0.023 0.002" rgba="0.30 0.32 0.36 1"/>
      <geom name="truck_{tid}_bed_np" type="box" pos="-0.033 0.047 {top - 0.002:.4f}" size="0.011 0.023 0.002" rgba="0.30 0.32 0.36 1"/>
      <geom name="truck_{tid}_bed_nn" type="box" pos="-0.033 -0.047 {top - 0.002:.4f}" size="0.011 0.023 0.002" rgba="0.30 0.32 0.36 1"/>
      <geom name="truck_{tid}_bed_beam" type="box" pos="0 0.12 {top - 0.008:.4f}" size="0.090 0.020 0.008" rgba="0.22 0.24 0.28 1"/>
      <geom name="truck_{tid}_cab" type="box" pos="0 0.22 0.0425" size="0.085 0.080 0.0425" rgba="0.80 0.22 0.18 1"/>
      <geom name="truck_{tid}_cargo_wall_p" type="box" pos="0.088 -0.03 0.115" size="0.004 0.130 0.035" rgba="0.55 0.58 0.62 1"/>
      <geom name="truck_{tid}_cargo_wall_n" type="box" pos="-0.088 -0.03 0.115" size="0.004 0.130 0.035" rgba="0.55 0.58 0.62 1"/>
      <geom name="truck_{tid}_cargo_front" type="box" pos="0 0.095 0.115" size="0.088 0.005 0.035" rgba="0.55 0.58 0.62 1"/>
      <geom name="truck_{tid}_cargo_roof" type="box" pos="0 -0.03 0.1525" size="0.092 0.140 0.0025" rgba="0.45 0.48 0.52 1"/>
      <geom name="truck_{tid}_wheel_fl" type="cylinder" pos="-0.065 0.16 0.022" size="0.022 0.012" quat="0.7071 0 0.7071 0" rgba="0.10 0.10 0.12 1"/>
      <geom name="truck_{tid}_wheel_fr" type="cylinder" pos="0.065 0.16 0.022" size="0.022 0.012" quat="0.7071 0 0.7071 0" rgba="0.10 0.10 0.12 1"/>
      <geom name="truck_{tid}_wheel_rl" type="cylinder" pos="-0.065 0.28 0.022" size="0.022 0.012" quat="0.7071 0 0.7071 0" rgba="0.10 0.10 0.12 1"/>
      <geom name="truck_{tid}_wheel_rr" type="cylinder" pos="0.065 0.28 0.022" size="0.022 0.012" quat="0.7071 0 0.7071 0" rgba="0.10 0.10 0.12 1"/>
    </body>"""


class TruckHandle:
    """单辆卡车句柄: mocap 索引 + 车体/条带 geom id。"""

    def __init__(self, spec: TruckSpec, bid: int, mocap_idx: int,
                 bed_gids: set[int]):
        self.spec = spec
        self.bid = bid
        self.mocap_idx = mocap_idx
        self.bed_gids = bed_gids

    @property
    def truck_id(self) -> str:
        return self.spec.truck_id


class TruckCell(WarehouseCell):
    """注入 BerthTruck 的 WarehouseCell (场景组合, 不改内核 XML 语义)。

    secure_with: 装车后随车运输的托盘体名清单 —— 生成 truck×pallet
    **weld 约束 (默认失活)**, 离场前由 runtime 激活对应 weld = 货物
    绑扎锁止。mocap 静体无速度场 (接触摩擦不携运), weld 是「载托随
    车」的唯一物理一致建模; 见交付边界说明。
    """

    def __init__(self, *, trucks: tuple[TruckSpec, ...] = (),
                 secure_with: tuple[str, ...] = (), **kw):
        self._truck_specs = tuple(trucks)
        self._secure_with = tuple(secure_with)
        self.trucks: dict[str, TruckHandle] = {}
        self._welds: dict[tuple[str, str], int] = {}
        super().__init__(**kw)

    def _xml(self, dt: float) -> str:
        xml = super()._xml(dt)
        bodies = "".join(
            _truck_body_xml(s, *berth_pose(self.layout, s.tk_node)[:2])
            for s in self._truck_specs)
        welds = "".join(
            f'\n    <weld name="secure_{s.truck_id}_{p}" body1="truck_{s.truck_id}" body2="{p}" active="false"/>'
            for s in self._truck_specs for p in self._secure_with)
        eq = f"\n  <equality>{welds}\n  </equality>" if welds else ""
        xml = xml.replace("  </worldbody>",
                          bodies + "\n  </worldbody>", 1)
        return xml.replace("</mujoco>", f"{eq}\n</mujoco>", 1) \
            if eq else xml

    def secure(self, truck_id: str, pallet_id: str, on: bool = True) -> None:
        """货物绑扎锁止 (离场前激活 weld; 卸场/空车不激活)。"""
        key = (truck_id, pallet_id)
        if key not in self._welds:
            raise KeyError(f"未配置绑扎约束: {key}")
        self.data.eq_active[self._welds[key]] = 1 if on else 0

    def _cache_ids(self):
        super()._cache_ids()
        m, T = self.model, mujoco.mjtObj
        for (tid, pid) in ((s.truck_id, p) for s in self._truck_specs
                           for p in self._secure_with):
            eid = mujoco.mj_name2id(m, T.mjOBJ_EQUALITY,
                                    f"secure_{tid}_{pid}")
            if eid < 0:
                raise KeyError(f"绑扎约束未注入: secure_{tid}_{pid}")
            self._welds[tid, pid] = eid
        for spec in self._truck_specs:
            bid = mujoco.mj_name2id(m, T.mjOBJ_BODY, f"truck_{spec.truck_id}")
            if bid < 0:
                raise KeyError(f"卡车体未注入: {spec.truck_id}")
            midx = m.body_mocapid[bid]
            bed = set()
            for g in range(m.ngeom):
                if m.geom_bodyid[g] == bid:
                    name = mujoco.mj_id2name(m, T.mjOBJ_GEOM, g) or ""
                    if "_bed_p" in name or "_bed_n" in name:
                        bed.add(g)
            self.trucks[spec.truck_id] = TruckHandle(spec, bid, midx, bed)

    # ------------------------------------------------------------ 驱动
    def place_truck(self, truck_id: str, xy: tuple[float, float]) -> None:
        h = self.trucks[truck_id]
        self.data.mocap_pos[h.mocap_idx][0] = xy[0]
        self.data.mocap_pos[h.mocap_idx][1] = xy[1]

    def truck_xy(self, truck_id: str) -> tuple[float, float]:
        p = self.data.mocap_pos[self.trucks[truck_id].mocap_idx]
        return float(p[0]), float(p[1])

    def bed_rect(self, truck_id: str) -> tuple[float, float, float, float]:
        """停靠位车斗矩形 (评分器 custody 位置核对用)。"""
        x, y = berth_pose(self.layout, self.trucks[truck_id].spec.tk_node)[:2]
        hx, hy = BED_HALF_XY
        return (x - hx, x + hx, y - hy, y + hy)
