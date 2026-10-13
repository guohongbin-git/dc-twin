"""线边库 L-B1..B3 —— 2 层小架 (蓝图 §13 产线供料闭环第 1 环;
§14 叙事: 装瓶厂瓶坯/物料线边缓存)。

几何参照 core warehouse_cell 货架/条带链生成, 但为「过桥门架」形制:
AGV 顶升交接面 L0 与 stand 同链 (顶 0.095, AGV 钻入架下顶销交接),
上层 L1 为缓冲层 —— 2 层小架, 立柱全部落在 AGV 停位点包络之外
(对角立柱, 内面距架心 ≥0.19 > 盘半径 0.12 + 余量)。

节点经 core register_node_source 注册 (布局可扩展点; 不进内核主
路径, 调度层 _layout_index 自动并入)。绑定关系见 line.registry。
"""
from __future__ import annotations

from dataclasses import dataclass

from sim.warehouse_cell import MatrixLayout, register_node_source

#: 产线走廊 = dock_y 东延线 (r=4 泊位带的场外延伸; 走廊以南为产出区)
LINE_CORRIDOR_Y = 2.10
#: L0 承托面顶 (同 stand_top_z / truck bed: AGV 顶销交接链)
L0_TOP_Z = 0.095
#: L1 上层承托面顶 (层高 0.15, 同 core 货架层距)
L1_TOP_Z = 0.245
#: 架位 x (东场外, 间距 0.55; L-B1 为 ST-PRESS 主供料位)
RACK_XS = (2.50, 3.05, 3.60)
_RACK_NODES = ("L-B1", "L-B2", "L-B3")

_REGISTERED = False


def lb_nodes(lay: MatrixLayout) -> dict[str, tuple[float, float, float]]:
    """节点源契约: 纯函数, 架心 = AGV 交接点 (销盘孔心)。"""
    return {name: (x, LINE_CORRIDOR_Y, 0.0)
            for name, x in zip(_RACK_NODES, RACK_XS)}


def register() -> dict[str, tuple[float, float, float]]:
    """注册进 core 节点源注册表 (幂等); 返回当前 L-B 节点快照。"""
    global _REGISTERED
    if not _REGISTERED:
        register_node_source(lb_nodes)
        _REGISTERED = True
    return lb_nodes(MatrixLayout())


@dataclass(frozen=True)
class RackGeometry:
    """单座 2 层小架的参数化几何 (XML 生成 + 评分器矩形核对)。"""

    node: str
    xy: tuple[float, float]

    @property
    def x(self) -> float:
        return self.xy[0]

    @property
    def y(self) -> float:
        return self.xy[1]

    def strip_positions(self) -> list[tuple[float, float, float]]:
        """承托条带 (L0+L1): (x, y, 顶面 z), 孔心 = 架心 (同 truck bed)。"""
        out = []
        for top in (L0_TOP_Z, L1_TOP_Z):
            for sx in (-0.033, 0.033):
                for sy in (-0.047, 0.047):
                    out.append((self.x + sx, self.y + sy, top))
        return out

    def leg_positions(self) -> list[tuple[float, float]]:
        """对角立柱 (z 通高): 距架心 ≥0.19 (AGV 包络外)。"""
        return [(self.x + sx, self.y + sy)
                for sx in (-0.19, 0.19) for sy in (-0.10, 0.10)]

    def xml(self) -> str:
        """注入 WarehouseCell 场景的 MJCF 片段 (静态门架体)。"""
        n = self.node
        parts = [f'  <body name="rack_{n}" pos="{self.x:.6f} {self.y:.6f} 0">']
        for sx in (-0.033, 0.033):
            for sy in (-0.047, 0.047):
                for top, tag in ((L0_TOP_Z, "strip"), (L1_TOP_Z, "upper")):
                    parts.append(
                        f'    <geom name="rack_{n}_{tag}_{"p" if sy > 0 else "n"}'
                        f'{"p" if sx > 0 else "n"}" type="box" '
                        f'pos="{sx:.3f} {sy:.3f} {top - 0.002:.4f}" '
                        f'size="0.011 0.023 0.002" '
                        f'rgba="{"0.80 0.42 0.10 1" if tag == "strip" else "0.45 0.52 0.60 1"}"/>')
        # 纵向弦杆 (条带/上层支承视觉链) + 对角立柱
        for sx in (-0.19, 0.19):
            for zc, half in ((0.0910, 0.0020), (0.2410, 0.0020)):
                parts.append(
                    f'    <geom name="rack_{n}_string{"L" if zc < 0.15 else "U"}'
                    f'{"p" if sx > 0 else "n"}" type="box" '
                    f'pos="{sx:.3f} 0 {zc:.4f}" size="0.006 0.110 {half:.4f}" '
                    f'rgba="0.30 0.32 0.36 1"/>')
        for lx, ly in self.leg_positions():
            parts.append(
                f'    <geom name="rack_{n}_leg_{lx:.2f}_{ly:.2f}" type="box" '
                f'pos="{lx - self.x:.3f} {ly - self.y:.3f} 0.1205" '
                f'size="0.006 0.006 0.1205" rgba="0.22 0.24 0.28 1"/>')
        parts.append("  </body>")
        return "\n".join(parts)
