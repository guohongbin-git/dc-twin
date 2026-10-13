"""P0 闭环运行时: 月台+卡车载荷闭环 (蓝图 §3-P0)。

一幕: 卡车按时刻表到站 (mocap 驶入) → 台账 prepare → AGV 顶升载托
走 **load 腿** (dock → tk) → 落托车斗 → 稳定窗采样 → 装车证据事件
(t_capture/t_ingest/t_decide) → 台账 commit/ack → 卡车离场 (载托随
车)。全程 sim_t 单时钟; 真值通道 (轨迹/物理 settle) 独立导出, 供
dc_twin.scorer 离线核对 —— 运行时不做「账实一致」裁决 (C2)。

六类负向注入 (faults): forge_commit / wrong_truck / crash_before_commit
/ ack_lost / duplicate_event / depart_unsettled。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sim.controllers import Ramp
from sim.demand import DemandEngine
from sim.navigation import FleetRunner
from sim.pallet_support import ledger_from_cell
from sim.warehouse_cell import K, MatrixLayout
from services.world.evidence import (FRAME_INTERVAL_S, MAX_SPEED,
                                     STABLE_WINDOW_S, EvidenceFrame,
                                     build_verdict, collect_frames)

from . import evidence as _ev
from . import layout as _tk
from . import legs as _legs
from . import core_version
from .ledger import ShipmentLedger
from .timetable import ShipmentOrder, TruckTimetable
from .trucks import BED_TOP_Z, STAGING_Y, TruckCell, TruckSpec

#: 需求引擎地平线 (覆盖时刻表全部到站)
HORIZON_S = 60.0
#: 装车载具 (P0 单 AGV 幕)
CARRIER = "A02"

FAULTS = ("forge_commit", "wrong_truck", "crash_before_commit", "ack_lost",
          "duplicate_event", "depart_unsettled")

DEFAULT_TIMETABLE = TruckTimetable((
    ("TRK01", "tk:T42", "D_G42", 2.0, 30.0),
    ("TRK02", "tk:T43", "D_G43", 8.0, 40.0),
))


class _InjectedCrash(RuntimeError):
    """负向用例 #3: 提交前进程崩溃注入。"""


@dataclass(frozen=True)
class RunResult:
    run_id: str
    out_dir: Path

    @property
    def truth(self) -> dict:
        return json.loads((self.out_dir / "truth.json").read_text())

    @property
    def ledger_doc(self) -> dict:
        return json.loads((self.out_dir / "ledger.json").read_text())

    @property
    def ops(self) -> list[dict]:
        return self.truth["ops"]

    @property
    def loaded_departed_closed(self) -> int:
        """装车∧离场∧台账闭合 的装运数 (运行时侧遥测汇总)。"""
        committed = {e["operation_id"] for e in self.ledger_doc["events"]
                     if e["type"] == "LoadCommitted"}
        departed = {t["truck_id"] for t in self.truth["trucks"]
                    if t["departed_t"] is not None}
        return sum(1 for o in self.ops
                   if o["truth_state"] == "loaded"
                   and o["truck_id"] in departed
                   and o["operation_id"] in committed)

    @property
    def nonatomic_window_s(self) -> float:
        """非原子窗口上界遥测: max(t_commit − truth_settle)。"""
        commit = {e["operation_id"]: e["ts_s"] for e in
                  self.ledger_doc["events"] if e["type"] == "LoadCommitted"}
        wins = [commit[o["operation_id"]] - o["truth_settle_time"]
                for o in self.ops
                if o["operation_id"] in commit
                and o["truth_settle_time"] is not None]
        return round(max(wins), 6) if wins else 0.0


# ---------------------------------------------------------------- 物理
def _probe(cell, pallet: str, bed_gids: set[int], pin_gid: int) -> EvidenceFrame:
    """证据帧探针 (纯物理观测; 同时钟 sim_t)。"""
    pos = cell.pallet_pose(pallet)[0]
    m = cell.model
    jadr = m.jnt_dofadr[m.body_jntadr[cell.pallets[pallet].bid]]
    v = float(np.linalg.norm(cell.data.qvel[jadr:jadr + 3]))
    return EvidenceFrame(t=cell.data.time,
                         new_support=cell.surface_load_ratio(pallet, bed_gids),
                         old_support=cell.surface_load_ratio(pallet, {pin_gid}),
                         pallet_x=float(pos[0]), pallet_y=float(pos[1]),
                         pallet_z=float(pos[2]), speed=v)


def _load_segments(cell, tk_y: float) -> tuple[list, list, list]:
    """load 腿物理段: [顶升, 载运] | [落托] | [回泊 dock]。"""
    view = cell.veh[CARRIER]
    cp = cell.vclass(CARRIER)
    pin_up = (view.a["pin"],
              lambda: Ramp(view.q("pin"), K["pin_travel"], cp["lift"], 0.10),
              view.off["pin"], 0.30)
    carry = (view.a["y"],
             lambda: Ramp(view.q("y") + view.off["y"], tk_y, 0.10, 0.30),
             view.off["y"], 0.20)
    pin_dn = (view.a["pin"],
              lambda: Ramp(view.q("pin"), 0.0, 0.010, 0.10),
              view.off["pin"], 0.30)
    back = (view.a["y"],
            lambda: Ramp(view.q("y") + view.off["y"], cell.layout.dock_y,
                         0.10, 0.30),
            view.off["y"], 0.20)
    return [pin_up, carry], [pin_dn], [back]


def _run_segments(cell, runner: FleetRunner, vid: str, segs: list) -> None:
    runner.submit(vid, segs)
    while runner.busy(vid):
        runner.run_until_any_idle()


def _wait_static(cell, pallet: str, timeout_s: float = 4.0) -> None:
    """等托盘物理静止 (稳定窗前置; 上界受监控)。"""
    m = cell.model
    jadr = m.jnt_dofadr[m.body_jntadr[cell.pallets[pallet].bid]]
    t_end = cell.data.time + timeout_s
    while cell.data.time < t_end:
        if float(np.linalg.norm(cell.data.qvel[jadr:jadr + 3])) < 0.4 * MAX_SPEED:
            return
        cell.step(10)


def _animate_truck(cell, truck_id: str, y_to: float, *,
                   speed: float = 0.30) -> float:
    """mocap 驶入/驶离 (smoothstep 限加加速度); 返回完成时刻 sim_t。"""
    h = cell.trucks[truck_id]
    y0 = float(cell.data.mocap_pos[h.mocap_idx][1])
    dur = abs(y_to - y0) / speed
    n = max(1, int(dur / 0.01))
    for i in range(1, n + 1):
        u = i / n
        cell.data.mocap_pos[h.mocap_idx][1] = \
            y0 + (y_to - y0) * (u * u * (3.0 - 2.0 * u))
        cell.step(10)
    cell.data.mocap_pos[h.mocap_idx][1] = y_to
    return cell.data.time


_FRAME_HOOK = None   # run_p0 独占流程的渲染钩子 (render_p0_demo 注入)

def _advance(cell, t_target: float) -> None:
    while cell.data.time < t_target:
        cell.step(50)
        if _FRAME_HOOK is not None:
            _FRAME_HOOK()


def _forged_frames(cell, tk_xy: tuple[float, float]) -> tuple[EvidenceFrame, ...]:
    """负向用例 #1: 未装车伪造证据 (支撑/位姿/静止全绿)。

    伪造帧必须落在当前时刻**之前** (谎称「已观测完成」), 伪造的时间
    三元组才与真实事务流程同构 —— 单调性由此仍成立 (伪造的是观测,
    不是时钟)。"""
    t0 = cell.data.time
    return tuple(EvidenceFrame(t=t0 - STABLE_WINDOW_S + i * FRAME_INTERVAL_S,
                               new_support=1.0, old_support=0.0,
                               pallet_x=tk_xy[0], pallet_y=tk_xy[1],
                               pallet_z=BED_TOP_Z + 0.001, speed=0.0)
                 for i in range(11))


# ---------------------------------------------------------------- 主幕
def run_p0(*, faults=(), out_dir=None, timetable: TruckTimetable | None = None,
           run_id: str | None = None, render_dir: str | None = None,
           render_every: float = 0.5) -> RunResult:
    faults = frozenset(faults)
    unknown = faults - set(FAULTS)
    if unknown:
        raise ValueError(f"未知注入: {sorted(unknown)}; 可用 {FAULTS}")
    run_id = run_id or f"P0-{uuid.uuid4().hex[:8]}"
    out_dir = Path(out_dir) if out_dir else Path("reports") / "p0" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # 场景载荷注册 (core 可扩展点)
    _tk.register()
    _legs.register()
    _ev.register()
    timetable = timetable or DEFAULT_TIMETABLE

    # 布景: AGV 直接泊于 D_G42 (托盘预载交付台; 载荷闭环不含厂内取段)
    lay = MatrixLayout(seed_pallets=2)
    dock_xy = lay.dock_poses()["D_G42"]
    lay.p["park"] = {**lay.p["park"], CARRIER: (*dock_xy[:2], 0.0)}
    cell = TruckCell(
        layout=lay, trucks=(TruckSpec("TRK01", "tk:T42"),
                            TruckSpec("TRK02", "tk:T43")),
        secure_with=("pallet_D_G42",),
        forklifts=("F01",), agvs=(CARRIER,),
        preload_stands=("D_G42",), seed_slots=[])
    cell.warmup(0.5)
    frame_hook = None
    if render_dir:
        import mujoco
        from pathlib import Path as _P
        _rd = _P(render_dir); _rd.mkdir(parents=True, exist_ok=True)
        _renderer = mujoco.Renderer(cell.model, height=720, width=1280)
        _cam = mujoco.MjvCamera()
        _cam.type = mujoco.mjtCamera.mjCAMERA_FREE
        _cam.lookat[:] = (0.0, 0.4, 0.0); _cam.distance = 5.2
        _cam.azimuth = 155; _cam.elevation = -38
        _state = {"next": 0.0, "n": 0}
        def frame_hook():
            if cell.data.time >= _state["next"]:
                _renderer.update_scene(cell.data, camera=_cam)
                import PIL.Image
                PIL.Image.fromarray(_renderer.render()).save(
                    _rd / f"f_{_state['n']:04d}.png")
                _state["n"] += 1
                _state["next"] = cell.data.time + render_every
        global _FRAME_HOOK
        _FRAME_HOOK = frame_hook
    runner = FleetRunner(cell)
    ledger = ShipmentLedger()

    # 时刻表 → DemandEngine 外部到达源 (core 可扩展点; 引擎只管归并)
    engine = DemandEngine(initial_pallets=64, arrival_sources=(timetable,))
    shipments = [s for s in engine.generate_until(HORIZON_S)
                 if isinstance(s, ShipmentOrder)]

    # 交付台库存 (真值侧; P0 预载 D_G42)
    dock_inventory: dict[str, str] = {
        w[len("dock:"):]: p for p, w in ledger_from_cell(cell).loc.items()
        if w.startswith("dock:")}

    # 真值通道 (独立导出; 评分器唯一物理事实来源)
    truth = {"run_id": run_id, "core_version": core_version(),
             "clock_domain": "sim_t", "ops": [], "trucks": [
                 {"truck_id": tid, "tk_node": h.spec.tk_node,
                  "berthed_t": None, "departed_t": None,
                  "bed_rect": list(cell.bed_rect(tid)), "pos_timeline": []}
                 for tid, h in cell.trucks.items()]}
    truck_state = {t["truck_id"]: t for t in truth["trucks"]}
    events_out: list = []
    seq = 0

    def _sample_traj(traj: list, pallet: str) -> None:
        xyz = cell.pallet_pose(pallet)[0]
        traj.append({"t": round(cell.data.time, 4),
                     "x": round(float(xyz[0]), 5),
                     "y": round(float(xyz[1]), 5),
                     "z": round(float(xyz[2]), 5)})

    def _arrive(visit) -> None:
        st = truck_state[visit.truck_id]
        if st["berthed_t"] is None:
            _advance(cell, visit.t_arrive_s)       # 按时刻表到站
            st["pos_timeline"].append(
                {"t": round(cell.data.time, 4), "y": STAGING_Y})
            st["berthed_t"] = _animate_truck(cell, visit.truck_id,
                                             _berth_y(visit))
            st["pos_timeline"].append(
                {"t": round(st["berthed_t"], 4),
                 "y": round(cell.truck_xy(visit.truck_id)[1], 4)})

    def _berth_y(visit) -> float:
        return berth_xy(cell, visit.tk_node)[1]

    def _depart(visit, cargo: str | None = None) -> None:
        st = truck_state[visit.truck_id]
        if st["departed_t"] is None:
            _advance(cell, visit.t_depart_s)
            if cargo is not None:
                cell.secure(visit.truck_id, cargo)   # 货物绑扎: 载托随车
            st["departed_t"] = _animate_truck(cell, visit.truck_id, STAGING_Y)
            st["pos_timeline"].append(
                {"t": round(st["departed_t"], 4), "y": STAGING_Y})

    try:
        for shp in shipments:
            visit = timetable.visit_of(shp.truck_id)
            pallet = dock_inventory.pop(shp.dock_node, None)
            _arrive(visit)
            if pallet is None:
                _depart(visit)                  # 空车到访: 到站即离场
                continue
            # ---- prepare ----
            op_id = f"OP-{shp.order_id}"
            t_prepare = cell.data.time
            ledger.prepare(op_id, pallet_id=pallet, truck_id=shp.truck_id,
                           tk_node=shp.tk_node, dock_node=shp.dock_node,
                           t_s=t_prepare)
            ledger.snapshot(t_s=t_prepare)
            lease = f"{shp.tk_node}#{op_id}"
            traj: list = []
            _sample_traj(traj, pallet)
            # ---- load 腿 (物理) ----
            if "forge_commit" in faults:
                frames = _forged_frames(cell, berth_xy(cell, shp.tk_node))
                truth_event_time = None
                truth_settle_time = None
            else:
                up, dn, back = _load_segments(cell, _berth_y(visit))
                _run_segments(cell, runner, CARRIER, up)
                _sample_traj(traj, pallet)
                truth_event_time = cell.data.time      # 落托起始 = 物理事件
                _run_segments(cell, runner, CARRIER, dn)
                _wait_static(cell, pallet)
                frames = tuple(collect_frames(
                    step=lambda: cell.step(50),
                    probe=lambda: _probe(cell, pallet, _bed_gids(cell, shp),
                                         cell.veh[CARRIER].pin_gid),
                    duration_s=STABLE_WINDOW_S,
                    interval_s=FRAME_INTERVAL_S))
                truth_settle_time = frames[0].t
                _sample_traj(traj, pallet)
            verdict = build_verdict(
                verdict_id=f"{run_id}-EVD-{seq + 1:03d}", kind="LOAD",
                task_id=shp.order_id, pallet_id=pallet,
                vehicle_id=CARRIER, lease_id=lease, frames=frames)
            # ---- ingest (sim_t 单时钟推进) ----
            cell.step(20)
            t_ingest = cell.data.time
            if "crash_before_commit" in faults:
                truth["ops"].append(_truth_op(shp, op_id, pallet, traj,
                                              truth_event_time,
                                              truth_settle_time, cell))
                raise _InjectedCrash(op_id)
            # ---- decide / commit ----
            cell.step(20)
            t_decide = cell.data.time
            do_commit = "depart_unsettled" not in faults
            if do_commit:
                ledger.commit(op_id, t_s=t_decide,
                              evidence_ref=verdict.verdict_id,
                              truck_id="TRK02" if "wrong_truck" in faults
                              else None)
                if "duplicate_event" in faults:
                    ledger.commit(op_id, t_s=t_decide + 0.1,
                                  evidence_ref=verdict.verdict_id,
                                  _duplicate=True)
            seq += 1
            event = _ev.build_load_event(
                event_id=f"{run_id}-EVT-{seq:03d}", operation_id=op_id,
                pallet_id=pallet, truck_id=shp.truck_id, tk_node=shp.tk_node,
                dock_node=shp.dock_node, task_id=shp.order_id,
                vehicle_id=CARRIER, lease_id=lease, frames=frames,
                t_ingest=t_ingest, t_decide=t_decide,
                verdict_failures=verdict.failures,
                estimated_event_time=((round(truth_event_time + 0.05, 4), 0.2)
                                      if truth_event_time is not None else None))
            events_out.append(event)
            if do_commit and "ack_lost" not in faults:
                cell.step(10)
                ledger.ack(op_id, t_s=cell.data.time)
            op = _truth_op(shp, op_id, pallet, traj, truth_event_time,
                           truth_settle_time, cell)
            xyz = cell.pallet_pose(pallet)[0]
            op["pallet_xyz_at_commit"] = [round(float(v), 5) for v in xyz]
            truth["ops"].append(op)
            ledger.snapshot(t_s=cell.data.time)
            # ---- AGV 退场回泊 + 卡车离场 (载托随车) ----
            if "forge_commit" not in faults:
                _run_segments(cell, runner, CARRIER, back)
            _depart(visit, cargo=pallet if "forge_commit" not in faults
                    else None)
            op["final_pallet_xyz"] = [round(float(v), 5)
                                      for v in cell.pallet_pose(pallet)[0]]
    except _InjectedCrash:
        ledger.snapshot(t_s=cell.data.time)        # 崩溃边界快照

    (out_dir / "truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "ledger.json").write_text(
        json.dumps(ledger.export(run_id=run_id), ensure_ascii=False, indent=1),
        encoding="utf-8")
    (out_dir / "evidence_events.json").write_text(
        json.dumps([json.loads(e.to_json()) for e in events_out],
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return RunResult(run_id=run_id, out_dir=out_dir)


def berth_xy(cell: TruckCell, tk_node: str):
    from .trucks import berth_pose

    return berth_pose(cell.layout, tk_node)


def _bed_gids(cell: TruckCell, shp: ShipmentOrder) -> set[int]:
    return cell.trucks[shp.truck_id].bed_gids


def _truth_op(shp: ShipmentOrder, op_id: str, pallet: str, traj: list,
              truth_event_time, truth_settle_time, cell) -> dict:
    """真值通道单据: 只含物理事实 (哪辆车的车斗真装了托盘由 bed_rect
    包含关系物理判定), 不含台账/业务状态。"""
    loaded = False
    physical_truck = None
    xyz = cell.pallet_pose(pallet)[0]
    for tid in cell.trucks:
        x0, x1, y0, y1 = cell.bed_rect(tid)
        if x0 <= float(xyz[0]) <= x1 and y0 <= float(xyz[1]) <= y1 \
                and float(xyz[2]) > 0.05:
            loaded, physical_truck = True, tid
            break
    return {"operation_id": op_id, "order_id": shp.order_id,
            "pallet_id": pallet, "truck_id": physical_truck,
            "intended_truck_id": shp.truck_id, "tk_node": shp.tk_node,
            "dock_node": shp.dock_node,
            "truth_state": "loaded" if loaded else "at_dock",
            "truth_event_time": truth_event_time,
            "truth_settle_time": truth_settle_time,
            "trajectory": traj}
