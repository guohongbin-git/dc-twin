"""LineCell —— 装瓶厂产线场景装配 (蓝图 §13/§14)。

在 WarehouseCell 生成的 XML 上注入 (场景组合, 不改内核语义):
- 线边库 L-B1..B3 (2 层小架, line.racks);
- ST-* 工位机构: 夹具 nest (背止挡定位) + 冲压头 slide actuator +
  推料滑台 + 夹紧 weld (默认失活, 激活即「夹紧确认」);
- 物流结构: 暂存门架 (新托盘+工件列) / 产出门架 (成品托盘列);
- 产线托盘 pl_* (自由体) + 工件 wp_* (自由体, 坐托盘顶)。

净空链 (全程无碰撞走廊): AGV 顶 0.062 < 条带底 0.090; 立柱对角
布置, 距 AGV 停位点 ≥0.16 (盘 r0.12+余量); 托盘交接面全链 0.095
(同 stand/bed 顶销交接链)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import mujoco
import numpy as np

from sim.warehouse_cell import PalletView, WarehouseCell

from . import racks as _racks
from .registry import PressSpec, rack_bindings, station as _station_spec

#: 工位平面 (相对走廊线的托盘止点; 背止挡面 = 止点 +0.030)
NEST_STOP_DY = 0.225
#: 夹具承托面 (AGV 顶升焊携托制式平面)
PLANE_Z = _racks.L0_TOP_Z
#: 冲压头: 头底 q=0 = PLANE_Z+0.055 (坯顶 PLANE_Z+0.017 上方 38mm);
#: 下压行程 q ∈ [-PRESS_Q_RANGE, 0], 目标 q* 使头底 = 坯顶-2mm
#: (刚性坯压入 2mm, 力 = kp×|Δq|; 不可全压穿 —— 全压将坯顶入托盘
#: 卡死机构, 实测 pen 62mm 事故即此)
PRESS_Q_RANGE = 0.055
PRESS_Q_TARGET = -0.043
#: 工件 = 冲压板坯 (半尺寸; §14 叙事: 压盖/冲压类工位坯料)
WP_HALF_XYZ = (0.024, 0.018, 0.003)
WP_MASS_KG = 0.05
#: 产线薄顶托盘顶面 (core 托盘顶 0.020 → 0.010: 载托底 0.122 与
#: 在架坯顶 0.111 净空 11mm, 供料环不跨压在架料)
LPAL_TOP_DZ = 0.010
#: 暂存/产出门架。暂存列 x=3.30/4.15 (L-B2/L-B3 东侧, 供料环短);
#: 槽位距 0.30, 取料深先南后北 —— 携托凸台底 0.092 < 在架坯顶
#: 0.111, 不可跨未取位 (回程只穿已取空的南侧槽位); 北端 y=2.10 行
#: 弃用 (避 L-B* 门腿 y=2.00/2.20 行)。产出位回 L-B1 车道。
STAGING_X = 2.50              # 暂存列 = 供料摆线列 (A02 纯 Y 摆)
STAGING_PITCH = 0.30
STAGING_Y_TOP = 1.80          # 首槽 (北先浅取; 北向直上 nest 无跨位)
SLOTS_PER_BENCH = 10          # 单列容量 (y 1.80 → −0.90, AGV 轴程内)
OUT_X = 2.43                  # 产出列首槽 (A03 北Lane y=3.02 东延取位)
OUT_PITCH = 0.12
OUT_Y = 3.02                  # 产出行 (A03 携托偏置 −0.15 落位行)


def run_segments(cell, segs: list[tuple]) -> None:
    """单通道段执行器 (FleetRunner 同构: 100Hz 位置闭环 + settle)。"""
    sub = max(1, round(0.01 / cell.model.opt.timestep))
    for seg in segs:
        aid, build, off = seg[0], seg[1], seg[2]
        settle = seg[3] if len(seg) > 3 else 0.15
        ramp = build()
        t0 = cell.data.time
        while cell.data.time - t0 < ramp.duration:
            cell.data.ctrl[aid] = ramp.at(cell.data.time - t0) - off
            cell.step(sub)
        cell.data.ctrl[aid] = ramp.target - off
        for _ in range(max(1, int(settle / (sub * cell.model.opt.timestep)))):
            cell.step(sub)


@dataclass(frozen=True)
class StationGeometry:
    """工位平面几何 (nest/exit 止点矩形 + 机构锚点 + MJCF 生成)。"""

    spec: PressSpec
    rack_node: str

    @property
    def x(self) -> float:
        return _racks.register()[self.rack_node][0]

    @property
    def cy(self) -> float:
        return _racks.LINE_CORRIDOR_Y

    @property
    def nest_y(self) -> float:
        return self.cy + NEST_STOP_DY

    def nest_rect(self) -> tuple[float, float, float, float]:
        """nest 托盘止位矩形 (x0,x1,y0,y1) —— 评分器 custody 核对。"""
        return (self.x - 0.048, self.x + 0.048,
                self.nest_y - 0.034, self.nest_y + 0.034)

    def exit_rect(self) -> tuple[float, float, float, float]:
        """成品取出口 = nest 本位 (压制完成后 A03 原位取托)。"""
        return self.nest_rect()

    # ------------------------------------------------------------ MJCF
    def xml(self) -> str:
        x, sid = self.x, self.spec.station_id
        ny, cy = self.nest_y, self.cy
        g = [f'  <body name="line_fixt_{sid}" pos="0 0 0">']
        A = g.append
        for sx in (-0.033, 0.033):                      # nest 条带 (托盘座位)
            for sy in (ny - 0.047, ny + 0.047):
                A(f'    <geom name="fixt_{sid}_nest_{sx:.3f}_{sy:.3f}" '
                  f'type="box" pos="{x + sx:.3f} {sy:.3f} {PLANE_Z - 0.002:.4f}" '
                  f'size="0.011 0.023 0.002" rgba="0.32 0.46 0.52 1"/>')
        # nest 背止挡 → 双块 (x ±0.0625): 携托凸台 (x ±0.020, 底 0.046)
        # 从块间通过, 托板北面 (x ±0.055) 顶靠块内档 2.4525 定位
        for sx in (-0.0625, 0.0625):
            A(f'    <geom name="fixt_{sid}_stop_nest_{sx:.2f}" type="box" '
              f'pos="{x + sx:.3f} {ny + 0.035:.3f} {PLANE_Z - 0.0015:.4f}" '
              f'size="0.015 0.005 0.0025" rgba="0.85 0.30 0.20 1"/>')
        # 冲压门架 (portal 立柱距 A02 摆线 x=2.50 恒 0.16)
        for sx in (-0.16, 0.16):
            A(f'    <geom name="fixt_{sid}_leg_{sx:.2f}" type="box" '
              f'pos="{x + sx:.2f} {ny:.3f} 0.140" size="0.008 0.008 0.140" '
              f'rgba="0.22 0.24 0.28 1"/>')
        A(f'    <geom name="fixt_{sid}_beam" type="box" '
          f'pos="{x:.3f} {ny:.3f} 0.300" size="0.170 0.010 0.020" '
          f'rgba="0.30 0.32 0.36 1"/>')
        # 视觉层 (contype=0): 底座面板 + 控制柜 + 立柱安全黄黑饰带
        A(f'    <geom name="fixt_{sid}_basepanel" type="box" '
          f'pos="{x:.3f} {ny - 0.10:.3f} 0.020" size="0.150 0.060 0.020" '
          f'rgba="0.45 0.48 0.52 1" contype="0" conaffinity="0"/>')
        A(f'    <geom name="fixt_{sid}_cabinet" type="box" '
          f'pos="{x + 0.30:.3f} {ny:.3f} 0.100" size="0.050 0.060 0.100" '
          f'rgba="0.36 0.47 0.58 1" contype="0" conaffinity="0"/>')
        for zz in (0.10, 0.22):
            A(f'    <geom name="fixt_{sid}_stripe_{zz:.2f}" type="box" '
              f'pos="{x:.3f} {ny:.3f} {zz:.3f}" size="0.170 0.012 0.010" '
              f'rgba="0.95 0.79 0.04 1" contype="0" conaffinity="0"/>')
        # ---- 冲压头 (slide actuator; ctrlrange 显式) ----
        A(f'    <body name="line_press_{sid}" pos="{x:.3f} {ny:.3f} '
          f'{PLANE_Z + 0.065:.4f}">')
        A(f'      <joint name="line_{sid}_press_j" type="slide" axis="0 0 1" '
          f'range="{-PRESS_Q_RANGE} 0" damping="30"/>')
        A(f'      <geom name="line_{sid}_head" type="box" pos="0 0 0" '
          f'size="0.030 0.026 0.010" mass="0.30" '
          f'friction="0.9 0.005 0.0001" rgba="0.75 0.76 0.80 1"/>')
        A(f'      <geom name="line_{sid}_stem" type="cylinder" pos="0 0 0.085" '
          f'size="0.006 0.075" mass="0" rgba="0.45 0.46 0.50 1" '
          f'contype="0" conaffinity="0"/>')
        A("    </body>")
        A("  </body>")
        return "\n".join(g)

    def actuators_xml(self) -> str:
        sid = self.spec.station_id
        return (
            f'    <position name="line_{sid}_press" joint="line_{sid}_press_j" '
            f'kp="4000" forcerange="-80 80" '
            f'ctrlrange="{-PRESS_Q_RANGE} 0"/>')




#: AGV 干道走廊 (立柱净空带: 带内不设立柱, 静态门架视觉链可断)
BENCH_CORRIDOR_YS = (1.60, 2.10)


def _bench_xml_staging(tag: str, x: float, slot_ys: list[float]) -> str:
    """暂存列: 逐槽支承对 (块顶 0.095, 内档 0.034>销盘缘 0.026)。
    不设长轨 —— 携托凸台/销盘沿槽行自穿支承对间隙。"""
    parts = []
    for i, sy in enumerate(slot_ys):
        for sx in (-0.0475, 0.0475):
            parts.append(
                f'    <geom name="bench_{tag}_bearer_{i}_{sx:.2f}" '
                f'type="box" pos="{x + sx:.2f} {sy:.3f} 0.0885" '
                f'size="0.015 0.024 0.0065" rgba="0.28 0.30 0.34 1"/>')
    return f'  <body name="bench_{tag}" pos="0 0 0">\n' + \
        "\n".join(parts) + "\n  </body>"


def _bench_xml_x(tag: str, y: float, x_lo: float, x_hi: float,
                 corridor_ys: tuple[float, ...] = (),
                 rail_dy: float = 0.033) -> str:
    """横向门架 (导轨沿 x; 产出行)。立柱仅落在携托走廊带外。"""
    xmid, xhalf = (x_lo + x_hi) / 2, (x_hi - x_lo) / 2
    xs = [x_lo + 0.20]                # 首腿让开 A02 南下列 x=2.10
    while xs[-1] + 0.5 < x_hi - 1e-9:
        xs.append(xs[-1] + 0.5)
    if xs[-1] < x_hi - 1e-9:
        xs.append(x_hi)
    rails = "\n".join(
        f'    <geom name="bench_{tag}_rail_{sy:.2f}" type="box" '
        f'pos="{xmid:.3f} {y + sy:.3f} {PLANE_Z - 0.0025:.4f}" '
        f'size="{xhalf:.4f} 0.004 0.0025" rgba="0.28 0.30 0.34 1"/>'
        for sy in (-rail_dy, rail_dy))
    legs = []
    ly = y - 0.19 if all(abs((y - 0.19) - cy) >= 0.18
                         for cy in corridor_ys) else None
    ly2 = y + 0.19 if all(abs((y + 0.19) - cy) >= 0.18
                          for cy in corridor_ys) else None
    for ly in (ly, ly2):
        if ly is None:
            continue
        for lx in xs:
            legs.append(
                f'    <geom name="bench_{tag}_leg_{lx:.2f}_{ly:.2f}" '
                f'type="box" pos="{lx:.3f} {ly:.3f} 0.045" '
                f'size="0.006 0.006 0.045" rgba="0.22 0.24 0.28 1"/>')
    return (f'  <body name="bench_{tag}" pos="0 0 0">\n{rails}\n'
            + "\n".join(legs) + "\n  </body>")


def _bench_xml(tag: str, x: float, y_lo: float, y_hi: float,
               corridor_ys: tuple[float, ...] = ()) -> str:
    """暂存/产出门架: 导轨 (顶 0.095) + 对角立柱 (AGV 包络外; 携托
    凸台低至 0.092 —— 携托走廊带内不设立柱)。"""
    ymid, yhalf = (y_lo + y_hi) / 2, (y_hi - y_lo) / 2
    ys = [y_lo]
    while ys[-1] + 0.5 < y_hi - 1e-9:
        ys.append(ys[-1] + 0.5)
    if ys[-1] < y_hi - 1e-9:
        ys.append(y_hi)
    ys = [y for y in ys if all(abs(y - cy) >= 0.18
                               for cy in (*BENCH_CORRIDOR_YS,
                                           *corridor_ys))]
    rails = "\n".join(
        f'    <geom name="bench_{tag}_rail_{sx:.2f}" type="box" '
        f'pos="{x + sx:.2f} {ymid:.3f} {PLANE_Z - 0.0025:.4f}" '
        f'size="0.004 {yhalf:.4f} 0.0025" rgba="0.28 0.30 0.34 1"/>'
        for sx in (-0.037, 0.037))
    legs = "\n".join(
        f'    <geom name="bench_{tag}_leg_{sx:.2f}_{ly:.2f}" type="box" '
        f'pos="{x + sx:.2f} {ly:.3f} 0.045" size="0.006 0.006 0.045" '
        f'rgba="0.22 0.24 0.28 1"/>'
        for ly in ys for sx in (-0.19, 0.19))
    return f'  <body name="bench_{tag}" pos="0 0 0">\n{rails}\n{legs}\n  </body>'


def _pallet_slim_xml(name: str, x: float, y: float, z: float) -> str:
    """产线薄顶托盘: 底板/凸台同 core 链, 顶板 0.010 (core 0.020)。"""
    q = "1.0 0 0 0"
    return f"""
    <body name="{name}" pos="{x:.6f} {y:.6f} {z + 0.001:.6f}" quat="{q}">
      <freejoint name="free_{name}"/>
      <geom name="{name}_bottom_l" type="box" pos="0 -0.027 0.002" size="0.055 0.003 0.002" mass="0.008" rgba="0.55 0.40 0.22 1"/>
      <geom name="{name}_bottom_r" type="box" pos="0 0.027 0.002" size="0.055 0.003 0.002" mass="0.008" rgba="0.55 0.40 0.22 1"/>
      <geom name="{name}_bottom_c" type="box" pos="0 0 0.002" size="0.055 0.006 0.002" mass="0.012" rgba="0.55 0.40 0.22 1"/>
      <geom name="{name}_top_l" type="box" pos="0 -0.027 0.008" size="0.055 0.004 0.002" mass="0.008" friction="1.4 0.005 0.0001" rgba="0.72 0.55 0.32 1"/>
      <geom name="{name}_top_r" type="box" pos="0 0.027 0.008" size="0.055 0.004 0.002" mass="0.008" friction="1.4 0.005 0.0001" rgba="0.72 0.55 0.32 1"/>
      <geom name="{name}_top_c" type="box" pos="0 0 0.008" size="0.055 0.020 0.002" mass="0.010" friction="1.4 0.005 0.0001" rgba="0.72 0.55 0.32 1"/>
      <geom name="{name}_boss" type="box" pos="0 0 -0.015" size="0.020 0.020 0.015" mass="0.004" rgba="0.45 0.32 0.16 1"/>
    </body>"""


def _workpiece_xml(name: str, x: float, y: float, z: float) -> str:
    hx, hy, hz = WP_HALF_XYZ
    return f"""
    <body name="{name}" pos="{x:.6f} {y:.6f} {z:.6f}">
      <freejoint name="free_{name}"/>
      <geom name="{name}_body" type="box" pos="0 0 0" size="{hx} {hy} {hz}"
            mass="{WP_MASS_KG}" friction="1.0 0.005 0.0001" rgba="0.25 0.45 0.75 1"/>
    </body>"""


def _quat_conj(q: np.ndarray) -> np.ndarray:
    return np.array([q[0], -q[1], -q[2], -q[3]])


def _quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
                     w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                     w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])


@dataclass(frozen=True)
class PressHandle:
    """工位机构句柄: 执行器/关节/几何 id (运行时驱动 + 力采样)。"""

    spec: PressSpec
    geo: StationGeometry
    press_aid: int
    press_qadr: int
    head_bid: int
    head_gids: frozenset
    nest_strip_gids: frozenset
    fixture_bid: int
    welds: dict[str, int]

    @property
    def station_id(self) -> str:
        return self.spec.station_id

    def q_press(self, cell) -> float:
        return float(cell.data.qpos[self.press_qadr])

    def head_bottom_z(self, cell) -> float:
        return float(cell.data.xpos[self.head_bid][2]) - 0.010

    IN_POSITION_TOL = 6e-3      # 成形压紧时接触失步 ~4-5mm 属到位

    def in_position(self, cell) -> bool:
        """设备字判据: 冲压头进入成形带 (位置环; 不感知力值, 同 B4
        —— 设备字不读接触力, 容差覆盖压紧失步)。"""
        return abs(self.q_press(cell) - PRESS_Q_TARGET) < self.IN_POSITION_TOL


class LineCell(WarehouseCell):
    """注入产线场景的 WarehouseCell (装瓶厂产线供料, 蓝图 §13/§14)。"""

    def __init__(self, *, line_racks: tuple[str, ...] = ("L-B1", "L-B2", "L-B3"),
                 stations: tuple[str, ...] = ("ST-PRESS",),
                 line_pallets: int = 8,
                 preload_racks: tuple[str, ...] = ("L-B1", "L-B2"),
                 workpieces: bool = True, **kw):
        if line_pallets < len(preload_racks):
            raise ValueError("托盘数少于预载数")
        if line_pallets > SLOTS_PER_BENCH + len(preload_racks):
            raise ValueError(
                f"暂存容量不足 (单列 {SLOTS_PER_BENCH} 位:"
                f" 上限 {SLOTS_PER_BENCH + len(preload_racks)})")
        self._line_racks = tuple(line_racks)
        self._station_ids = tuple(stations)
        self._line_pallets = int(line_pallets)
        self._preload_racks = tuple(preload_racks)
        self._with_wp = bool(workpieces)
        self.geos: dict[str, StationGeometry] = {}
        self.presses: dict[str, PressHandle] = {}
        self.racks: dict[str, _racks.RackGeometry] = {}
        self.workpieces: dict[str, dict] = {}
        nodes = _racks.register()
        for node in self._line_racks:
            self.racks[node] = _racks.RackGeometry(node, nodes[node][:2])
        for sid in self._station_ids:
            spec = _station_spec(sid)
            rack = next(r for r, s in rack_bindings().items() if s == sid)
            self.geos[sid] = StationGeometry(spec, rack)
        super().__init__(**kw)

    # ------------------------------------------------------------ 播种
    def _pallet_names(self) -> list[str]:
        return []                       # 产线托盘由 _xml 直接注入

    @property
    def n_staging(self) -> int:
        return self._line_pallets - len(self._preload_racks)

    def staging_slot(self, k: int) -> tuple[float, float]:
        """暂存位 (A02 摆线列 x=STAGING_X; 北先浅取, 拾托后北向直上
        nest, 无跨位交叠)。"""
        if k >= self.n_staging:
            raise KeyError(f"暂存位不足: {k} ≥ {self.n_staging}")
        return (STAGING_X,
                STAGING_Y_TOP - STAGING_PITCH * (k % SLOTS_PER_BENCH))

    def out_slot(self, j: int) -> tuple[float, float]:
        """产出位 (y=OUT_Y 行, 自东 OUT_X 向西 0.12/槽 —— 东满优先,
        A03 携托西行永不跨已落位)。"""
        return (OUT_X + OUT_PITCH * j, OUT_Y)

    def _pallet_plan(self) -> list[tuple[str, float, float]]:
        """托盘播种表: 预载架位在前, 其余按暂存列序。"""
        out = [(f"pl_{k:03d}", *self.racks[node].xy)
               for k, node in enumerate(self._preload_racks)]
        out += [(f"pl_{len(self._preload_racks) + j:03d}", *self.staging_slot(j))
                for j in range(self.n_staging)]
        return out

    def pallet_of_wp(self, wp: str) -> str:
        return self.workpieces[wp]["pallet"]

    def geom_manifest(self) -> dict:
        """MJCF→three.js 几何镜像 (PanelTab schema v1; mujoco 约定
        half-size, 世界系 pos/quat 对静态体 = local; 动态体 dyn=true,
        每帧位姿走 dyn_geoms 按体驱动)。非原生 type (mesh/hfield) 跳过。"""
        m, d, T = self.model, self.data, mujoco.mjtObj
        mtype = {int(mujoco.mjtGeom.mjGEOM_PLANE): "plane",
                 int(mujoco.mjtGeom.mjGEOM_SPHERE): "sphere",
                 int(mujoco.mjtGeom.mjGEOM_CAPSULE): "capsule",
                 int(mujoco.mjtGeom.mjGEOM_CYLINDER): "cylinder",
                 int(mujoco.mjtGeom.mjGEOM_BOX): "box"}
        skip = {int(mujoco.mjtGeom.mjGEOM_MESH),
                int(mujoco.mjtGeom.mjGEOM_HFIELD),
                int(mujoco.mjtGeom.mjGEOM_ELLIPSOID),
                int(mujoco.mjtGeom.mjGEOM_SDF)}
        geoms, dyn_bodies = [], {}
        for g in range(m.ngeom):
            if int(m.geom_type[g]) in skip:
                continue
            bid = int(m.geom_bodyid[g])
            body = mujoco.mj_id2name(m, T.mjOBJ_BODY, bid) or ""
            gname = mujoco.mj_id2name(m, T.mjOBJ_GEOM, g) or ""
            gid = f"{body}:{gname or ('geom%d' % g)}"
            visual = bool(m.geom_contype[g] == 0
                          and m.geom_conaffinity[g] == 0)
            dyn = bool(m.body_dofnum[bid] > 0)
            if "stripe" in gname:
                mat = "safety"
            elif body.startswith("rack") or body.startswith("bench") \
                    or body.startswith("fixt") or gname.startswith("line_"):
                mat = "press" if "fixt" in body or gname.startswith("line_") \
                    else "rack"
            elif body.startswith(("A02", "A03")) or gname.startswith("A0"):
                mat = "agv"
            elif body.startswith("pl_") or gname.startswith("pl_") \
                    or gname.startswith("wp_"):
                mat = "pallet"
            elif gname == "floor":
                mat = "floor"
            else:
                mat = "steel"
            quat = [round(float(v), 6) for v in m.geom_quat[g]]
            geoms.append({
                "id": gid,
                "type": mtype[int(m.geom_type[g])],
                "size": [round(float(v), 6) for v in m.geom_size[g][:3]],
                "pos": [round(float(v), 6) for v in m.geom_pos[g]],
                "quat": quat if quat != [1.0, 0.0, 0.0, 0.0] else None,
                "rgba": [round(float(v), 4) for v in m.geom_rgba[g]],
                "material": mat,
                **({"dyn": True} if dyn else {})})
            if dyn and bid not in dyn_bodies:
                dyn_bodies[bid] = body
        dyn_geoms = [{"id": name,
                      "pos": [round(float(v), 6) for v in d.xpos[b]],
                      "quat": [round(float(v), 6) for v in d.xquat[b]]}
                     for b, name in sorted(dyn_bodies.items())]
        return {"format": "mjcf-mirror-v1", "geoms": geoms,
                "dyn_geoms": dyn_geoms}

    def pallet_of_wp(self, wp: str) -> str:
        return self.workpieces[wp]["pallet"]

    _MTYPE = {mujoco.mjtGeom.mjGEOM_PLANE: "plane",
              mujoco.mjtGeom.mjGEOM_HFIELD: "hfield",
              mujoco.mjtGeom.mjGEOM_SPHERE: "sphere",
              mujoco.mjtGeom.mjGEOM_CAPSULE: "capsule",
              mujoco.mjtGeom.mjGEOM_ELLIPSOID: "ellipsoid",
              mujoco.mjtGeom.mjGEOM_CYLINDER: "cylinder",
              mujoco.mjtGeom.mjGEOM_BOX: "box",
              mujoco.mjtGeom.mjGEOM_MESH: "mesh"}

    def geom_manifest(self) -> dict:
        """MJCF→three.js 几何镜像 (全 geom 清单, 维度取 MJCF 真值)。

        {format, bodies, geoms}: bodies = 位姿节点 (面板静态件一次
        下发; 动态件每帧按 name 查 dynamic_poses 更新); geoms = 视觉
        几何 (visual 几何 contype=0 已标注), rgba/material_class 供
        PBR 分色。material_class 按命名/用途归类 (工业配色)。"""
        m, d, T = self.model, self.data, mujoco.mjtObj
        bodies, geoms = [], []
        for b in range(m.nbody):
            name = mujoco.mj_id2name(m, T.mjOBJ_BODY, b)
            if not name or b == 0:
                continue
            bodies.append({
                "name": name,
                "parent": mujoco.mj_id2name(m, T.mjOBJ_BODY,
                                            m.body_parentid[b]) or "world",
                "pos": [round(float(v), 6) for v in d.xpos[b]],
                "quat": [round(float(v), 6) for v in d.xquat[b]],
                "dynamic": bool(m.body_dofnum[b] > 0
                                or m.body_weldid[b] != b)})
        mat_of = {"rack": "steel_frame", "bench": "steel_frame",
                  "fixt": "machine_frame", "line_press": "steel",
                  "line_push": "safety_yellow", "stg": "steel_frame",
                  "out": "steel_frame", "A02": "agv_blue",
                  "A03": "agv_blue", "pl_": "pallet_wood",
                  "wp_": "part_blank", "floor": "floor"}
        for g in range(m.ngeom):
            name = mujoco.mj_id2name(m, T.mjOBJ_GEOM, g)
            if not name:
                continue
            gname = name
            body = mujoco.mj_id2name(m, T.mjOBJ_BODY, m.geom_bodyid[g]) or ""
            visual = bool(m.geom_contype[g] == 0 and m.geom_conaffinity[g] == 0)
            mclass = "generic"
            for k, v in mat_of.items():
                if gname.startswith(k) or body.startswith(k):
                    mclass = v
                    break
            else:
                if "stripe" in gname:
                    mclass = "safety_yellow"
            geoms.append({
                "name": gname, "body": body,
                "type": self._MTYPE.get(int(m.geom_type[g]), "box"),
                "size": [round(float(v), 6) for v in m.geom_size[g]],
                "pos": [round(float(v), 6) for v in m.geom_pos[g]],
                "quat": [round(float(v), 6) for v in m.geom_quat[g]],
                "rgba": [round(float(v), 4) for v in m.geom_rgba[g]],
                "visual": visual,
                "material_class": mclass})
        return {"format": "mjcf-mirror-v1", "bodies": bodies, "geoms": geoms}

    def scene_static(self) -> dict:
        """静态几何 (面板 three.js 初始化一次下发; mujoco 世界系)。

        {plane, racks, station, staging, marks} —— 字段契约见
        dc_twin/line/runtime.py _LiveState.snapshot docstring。"""
        xs, ys = [], []
        rack_list = []
        for node, g in self.racks.items():
            rack_list.append({"node": node, "x": round(g.x, 4),
                              "y": round(g.y, 4), "w": 0.42, "d": 0.24,
                              "levels": [_racks.L0_TOP_Z, _racks.L1_TOP_Z]})
            xs += [g.x - 0.21, g.x + 0.21]
            ys += [g.y - 0.12, g.y + 0.12]
        for sid, geo in self.geos.items():
            xs += [geo.x - 0.18, geo.x + 0.18]
            ys += [geo.nest_y - 0.08, geo.nest_y + 0.08]
            nr = list(geo.nest_rect())
            press_sid = geo.spec.station_id
        staging = [{"x": round(self.staging_slot(k)[0], 4),
                    "y": round(self.staging_slot(k)[1], 4)}
                   for k in range(self.n_staging)]
        for x_s, y_s in [(s["x"], s["y"]) for s in staging]:
            xs += [x_s - 0.06, x_s + 0.06]
            ys += [y_s - 0.06, y_s + 0.06]
        n_out = max(1, self._line_pallets - 2)
        for j in range(n_out):
            x_o, y_o = self.out_slot(j)
            xs += [x_o - 0.07, x_o + 0.07]
            ys += [y_o - 0.06, y_o + 0.06]
        h = self.presses[press_sid]
        return {
            "plane": {"x": [round(min(xs), 4), round(max(xs), 4)],
                      "y": [round(min(ys), 4), round(max(ys), 4)]},
            "racks": rack_list,
            "station": {"id": press_sid,
                        "nest_rect": [round(v, 4) for v in nr],
                        "exit_rect": [round(v, 4) for v in nr],
                        "plunger": {
                            "x": round(geo.x, 4), "y": round(geo.nest_y, 4),
                            "w": 0.060, "d": 0.052,
                            "z_rest": round(PLANE_Z + 0.055, 4),
                            "z_bottom": round(PLANE_Z + 0.055
                                              - 0.055 + 0.002, 4)},
                        "backstop": {"x": round(geo.x, 4),
                                     "y": round(geo.nest_y + 0.035, 4),
                                     "w": 0.11, "d": 0.01}},
            "staging": staging,
            "marks": []}

    # ------------------------------------------------------------ MJCF
    def _xml(self, dt: float) -> str:
        xml = super()._xml(dt)
        xml = xml.replace('<size nconmax="600" njmax="400"/>',
                          '<size nconmax="2000" njmax="1200"/>', 1)
        bodies = [g.xml() for g in self.geos.values()]
        bodies += [self.racks[n].xml() for n in self._line_racks]
        n_out = max(1, self._line_pallets - 2)
        rows_a = [STAGING_Y_TOP - STAGING_PITCH * r
                  for r in range(self.n_staging)]
        bodies.append(_bench_xml_staging("stgA", STAGING_X, rows_a))
        bodies.append(_bench_xml_x(
            "out", OUT_Y, OUT_X - 0.05,
            OUT_X + OUT_PITCH * max(1, n_out - 1) + 0.05,
            corridor_ys=(OUT_Y, 3.30)))
        plan = self._pallet_plan()
        for name, x, y in plan:
            bodies.append(_pallet_slim_xml(name, x, y, PLANE_Z))
        if self._with_wp:
            for _name, x, y in plan:
                z = PLANE_Z + LPAL_TOP_DZ + WP_HALF_XYZ[2] + 0.003
                bodies.append(_workpiece_xml("wp_" + _name[-3:], x, y, z))
        # 夹紧 weld (每托盘 × 每实现工位, 默认失活; 编译期记录相对位形)
        welds = "".join(
            f'\n    <weld name="clamp_{sid}_{name}" body1="line_fixt_{sid}" '
            f'body2="{name}" active="false"/>'
            for sid, geo in self.geos.items() if geo.spec.implemented
            for name, _x, _y in plan)
        # 顶升 weld (虚拟抓手, P0 卡车绑扎同源): AGV 销盘体 ↔ 托盘,
        # 激活时锁当前位形 —— 拾放不受销行程/凸台悬挂几何约束
        welds += "".join(
            f'\n    <weld name="lift_{vid}_{name}" body1="{vid}_chassis" '
            f'body2="{name}" active="false"/>'
            for vid in self.agv_ids for name, _x, _y in plan)
        eq = f"\n  <equality>{welds}\n  </equality>" if welds else ""
        acts = "\n".join(g.actuators_xml() for g in self.geos.values())
        xml = xml.replace("  </worldbody>",
                          "".join("\n" + b for b in bodies) + "\n  </worldbody>", 1)
        xml = xml.replace("  </actuator>", acts + "\n  </actuator>", 1)
        return xml.replace("</mujoco>", f"{eq}\n</mujoco>", 1) if eq else xml

    # ------------------------------------------------------------ id 缓存
    def _cache_ids(self):
        super()._cache_ids()
        m, T = self.model, mujoco.mjtObj
        self.pallets = {}
        for name, _x, _y in self._pallet_plan():
            bid = mujoco.mj_name2id(m, T.mjOBJ_BODY, name)
            if bid < 0:
                raise KeyError(f"产线托盘未注入: {name}")
            gids = [g for g in range(m.ngeom) if m.geom_bodyid[g] == bid]
            self.pallets[name] = PalletView(name, bid, gids,
                                            float(m.body_mass[bid]))
        self.workpieces = {}
        if self._with_wp:
            for name, _x, _y in self._pallet_plan():
                wname = "wp_" + name[-3:]
                bid = mujoco.mj_name2id(m, T.mjOBJ_BODY, wname)
                if bid < 0:
                    raise KeyError(f"工件未注入: {wname}")
                self.workpieces[wname] = {
                    "bid": bid,
                    "gids": frozenset(g for g in range(m.ngeom)
                                      if m.geom_bodyid[g] == bid),
                    "pallet": name}
        for sid, geo in self.geos.items():
            s = geo.spec.station_id
            press_aid = mujoco.mj_name2id(m, T.mjOBJ_ACTUATOR, f"line_{s}_press")
            pj = mujoco.mj_name2id(m, T.mjOBJ_JOINT, f"line_{s}_press_j")
            hb = mujoco.mj_name2id(m, T.mjOBJ_BODY, f"line_press_{s}")
            fb = mujoco.mj_name2id(m, T.mjOBJ_BODY, f"line_fixt_{s}")
            if min(press_aid, pj, hb, fb) < 0:
                raise KeyError(f"工位机构未注入: {s}")
            hg = frozenset(g for g in range(m.ngeom)
                           if m.geom_bodyid[g] == hb
                           and (mujoco.mj_id2name(m, T.mjOBJ_GEOM, g) or "")
                           .endswith("_head"))
            nsg = frozenset(
                g for g in range(m.ngeom)
                if m.geom_bodyid[g] == fb
                and "_nest_" in (mujoco.mj_id2name(m, T.mjOBJ_GEOM, g) or ""))
            welds = {}
            if geo.spec.implemented:
                for name, _x, _y in self._pallet_plan():
                    eid = mujoco.mj_name2id(m, T.mjOBJ_EQUALITY,
                                            f"clamp_{s}_{name}")
                    if eid < 0:
                        raise KeyError(f"夹紧约束未注入: clamp_{s}_{name}")
                    welds[name] = eid
            self.presses[s] = PressHandle(
                spec=geo.spec, geo=geo, press_aid=press_aid,
                press_qadr=m.jnt_qposadr[pj],
                head_bid=hb, head_gids=hg, nest_strip_gids=nsg,
                fixture_bid=fb, welds=welds)
        self.lift_welds = {}
        for vid in self.agv_ids:
            for name, _x, _y in self._pallet_plan():
                eid = mujoco.mj_name2id(m, T.mjOBJ_EQUALITY,
                                        f"lift_{vid}_{name}")
                if eid < 0:
                    raise KeyError(f"顶升约束未注入: lift_{vid}_{name}")
                self.lift_welds[vid, name] = eid

    # ------------------------------------------------------------ 操作
    def _press_of(self, pallet: str) -> PressHandle:
        for h in self.presses.values():
            if pallet in h.welds:
                return h
        raise KeyError(f"未配置夹紧约束: {pallet}")

    def clamp(self, pallet: str, on: bool) -> None:
        """夹紧/松开。夹紧 = 在**当前位形**锁定 weld (夹具↔托盘):
        重写 eq_data (relpos = R1ᵀ(p2−p1), relquat = q1⁻¹∘q2, MuJoCo
        3.x weld 布局 [anchor|relpos|relquat|torquescale]) 后激活
        —— 夹紧确认即 eq_active 位, 且物理成立 (未夹紧时冲压可推移
        托盘); 松开仅失活。"""
        h = self._press_of(pallet)
        eid = h.welds[pallet]
        if on:
            m, d = self.model, self.data
            b1, b2 = h.fixture_bid, self.pallets[pallet].bid
            p1, p2 = d.xpos[b1], d.xpos[b2]
            r1 = d.xmat[b1].reshape(3, 3)
            q1, q2 = d.xquat[b1], d.xquat[b2]
            rel_q = _quat_mul(_quat_conj(q1), q2)
            ed = m.eq_data[eid]
            ed[:] = 0.0
            ed[3:6] = r1.T @ (p2 - p1)          # relpos (body1 局部)
            ed[6:10] = rel_q                    # relquat (body2←body1)
            ed[10] = 1.0                        # torquescale
            mujoco.mj_forward(m, d)
        self.data.eq_active[eid] = 1 if on else 0

    def clamp_active(self, pallet: str) -> bool:
        h = self._press_of(pallet)
        return bool(self.data.eq_active[h.welds[pallet]])

    def grip(self, vid: str, pallet: str, on: bool) -> None:
        """AGV 顶升 (虚拟抓手): 在**当前位形**锁定 pin 体 ↔ 托盘 weld
        (eq_data 同 clamp 配方)。拾/放不受销行程与凸台悬挂几何约束
        (P0 载托随车同源机制); on=False 仅失活, 托盘就地落座。"""
        eid = self.lift_welds.get((vid, pallet))
        if eid is None:
            raise KeyError(f"未配置顶升约束: lift_{vid}_{pallet}")
        if on:
            mujoco.mj_forward(self.model, self.data)   # 刷新 d.xpos/xmat
            m, d = self.model, self.data
            b1 = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY,
                                   f"{vid}_chassis")
            b2 = self.pallets[pallet].bid
            p1, p2 = d.xpos[b1], d.xpos[b2]
            r1 = d.xmat[b1].reshape(3, 3)
            q1, q2 = d.xquat[b1], d.xquat[b2]
            ed = m.eq_data[eid]
            ed[:] = 0.0
            ed[3:6] = r1.T @ (p2 - p1)          # relpos (body1 局部)
            ed[6:10] = _quat_mul(_quat_conj(q1), q2)
            ed[10] = 1.0
            mujoco.mj_forward(m, d)
        self.data.eq_active[eid] = 1 if on else 0

    def grip_active(self, vid: str, pallet: str) -> bool:
        return bool(self.data.eq_active[self.lift_welds[vid, pallet]])

    def wp_pose(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        w = self.workpieces[name]
        return (self.data.xpos[w["bid"]].copy(),
                self.data.xquat[w["bid"]].copy())

    def contact_force_n(self, station_id: str, wp_name: str) -> float:
        """冲压头 ↔ 工件 接触法向力合力 (力剖面采样原语)。"""
        h = self.presses[station_id]
        wg = self.workpieces[wp_name]["gids"]
        total, f1 = 0.0, np.zeros(6)
        for i in range(self.data.ncon):
            c = self.data.contact[i]
            if ((c.geom1 in h.head_gids and c.geom2 in wg)
                    or (c.geom2 in h.head_gids and c.geom1 in wg)):
                mujoco.mj_contactForce(self.model, self.data, i, f1)
                total += abs(float(f1[0]))
        return total

    def nest_rect(self, station_id: str) -> tuple[float, float, float, float]:
        return self.geos[station_id].nest_rect()

    def exit_rect(self, station_id: str) -> tuple[float, float, float, float]:
        return self.geos[station_id].exit_rect()
