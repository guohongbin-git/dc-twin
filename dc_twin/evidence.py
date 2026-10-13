"""装车证据事件 (四态之首; 蓝图 §3-P0 + C1 时间模型)。

运行时证据帧字段 = **t_capture / t_ingest / t_decide** (同时钟域
sim_t, 单调性在构造期强制); 可选 estimated_event_time 必须带不确定
性标注。真值时间 (truth_event_time / truth_settle_time /
truth_state / durable_commit_time) **不进运行时证据** —— 只存在于
独立评分器的真值导出 (dc_twin.runtime 导出 truth.json)。

事件类型 LOAD 经 core 事件类型注册表注册 (门判据全域共享不变)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from services.world import evidence as _ev

EVENT_SCHEMA = "dc_twin.load_event/1"
CLOCK_DOMAIN = "sim_t"
KIND_LOAD = "LOAD"

_REGISTERED = False


def register() -> None:
    """注册 LOAD 事件类型进 core (幂等)。"""
    global _REGISTERED
    if not _REGISTERED and KIND_LOAD not in _ev.EVENT_KINDS:
        _ev.register_event_kind(KIND_LOAD, "装车 (dock→tk, 四态之首)")
    _REGISTERED = True


@dataclass(frozen=True)
class LoadEvent:
    """一次装车的运行时证据事件 (纯数据, 可 JSON 序列化)。"""

    event_id: str
    schema: str
    kind: str
    clock_domain: str
    operation_id: str
    pallet_id: str
    truck_id: str
    tk_node: str
    dock_node: str
    task_id: str
    vehicle_id: str
    lease_id: str | None
    t_capture: float               # 末帧观测时刻 (sim_t)
    t_ingest: float                # 证据入账时刻 (sim_t)
    t_decide: float                # 裁决/提交时刻 (sim_t)
    estimated_event_time: tuple[float, float] | None    # (估计值, 不确定性)
    verdict: dict                  # {passed, failures, support, ...}
    frames: tuple[dict, ...]

    @property
    def passed(self) -> bool:
        return bool(self.verdict.get("passed"))

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(self.verdict.get("failures", ()))

    def to_json(self) -> str:
        d = dict(self.__dict__)
        d["estimated_event_time"] = (list(self.estimated_event_time)
                                     if self.estimated_event_time else None)
        d["frames"] = list(self.frames)
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "LoadEvent":
        d = json.loads(s)
        eet = d.get("estimated_event_time")
        return cls(**{**d, "estimated_event_time":
                      tuple(eet) if eet else None,
                      "frames": tuple(d["frames"])})


def build_load_event(*, event_id: str, operation_id: str, pallet_id: str,
                     truck_id: str, tk_node: str, dock_node: str,
                     task_id: str, vehicle_id: str, lease_id: str | None,
                     frames: tuple, t_ingest: float, t_decide: float,
                     verdict_failures: tuple[str, ...],
                     estimated_event_time: tuple[float, float] | None = None,
                     ) -> LoadEvent:
    """唯一构造入口: 时间三元组单调性 (同时钟域) + 证据过期在此强制。"""
    register()
    t_capture = float(frames[-1].t) if frames else float("nan")
    if not (0.0 <= t_capture <= t_ingest <= t_decide):
        raise ValueError(
            f"时间三元组违反单调性 (sim_t): t_capture={t_capture} "
            f"t_ingest={t_ingest} t_decide={t_decide}")
    if frames:
        caps = [f.t for f in frames]
        if caps != sorted(caps):
            raise ValueError("帧序列时间倒序")
    failures = list(verdict_failures)
    if frames and t_decide > frames[-1].t + _ev.VERDICT_TTL_S:
        failures.append(
            f"证据过期 (t_decide {t_decide:.3f} > valid_until "
            f"{frames[-1].t + _ev.VERDICT_TTL_S:.3f})")
    last = frames[-1] if frames else None
    verdict = {
        "passed": not failures,
        "failures": failures,
        "support": round(float(last.new_support), 4) if last else None,
        "old_support": round(float(last.old_support), 4) if last else None,
        "stable_window_s": (round(frames[-1].t - frames[0].t, 3)
                            if len(frames) > 1 else 0.0),
        "frames": len(frames),
    }
    return LoadEvent(
        event_id=event_id, schema=EVENT_SCHEMA, kind=KIND_LOAD,
        clock_domain=CLOCK_DOMAIN, operation_id=operation_id,
        pallet_id=pallet_id, truck_id=truck_id, tk_node=tk_node,
        dock_node=dock_node, task_id=task_id, vehicle_id=vehicle_id,
        lease_id=lease_id, t_capture=t_capture, t_ingest=float(t_ingest),
        t_decide=float(t_decide),
        estimated_event_time=(tuple(estimated_event_time)
                              if estimated_event_time else None),
        verdict=verdict,
        frames=tuple(f.to_dict() for f in frames))
