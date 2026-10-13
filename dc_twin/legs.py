"""load 腿类型 —— dock → tk 装车腿 (蓝图 §3-P0 第 2 条; 非 trunk)。

经 core register_leg_type 注册: 合格载具 = AGV (顶升载运), 端部作业
= 对中钻入 + 落托 (量级同 STAND_PICK_S); 调度/指派逻辑只读注册表,
内核 fork/trunk 语义零改动。
"""
from __future__ import annotations

from sim import dispatch

LOAD_LEG_TYPE = "load"
LOAD_HANDLING_S = 2.0                       # 对中钻入 + 顶落托 (s)

_REGISTERED = False


def register() -> None:
    global _REGISTERED
    if not _REGISTERED and LOAD_LEG_TYPE not in dispatch.LEG_VEHICLE_TYPE:
        dispatch.register_leg_type(LOAD_LEG_TYPE, vehicle_type="agv",
                                   handling_s=LOAD_HANDLING_S)
    _REGISTERED = True
