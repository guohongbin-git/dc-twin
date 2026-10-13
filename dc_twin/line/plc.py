"""工位 PLC 状态字 —— 设备侧唯一完成来源 (B4 _PlcStub 模式移植;
experiments/fault_ablation.py:660 只抄模式不 import 实验)。

「到点位」由设备自身动作程序置位 (机构位置环 in-position), 与
observed 传感器通道和物理真值严格分道: 本模块不读 cell.observed、
不读 cell.truth。设备字不感知力/接触 —— 力剖面证据由独立采样帧
承担 (dc_twin.line.evidence), 这是 PLC 完成字与证据门控的分界。
"""
from __future__ import annotations

from typing import Callable


class LinePlc:
    """工位状态字: op_id → BUSY | DONE; 有限重询 + 仿真退避。"""

    ACK_POLL_MAX = 3            # 有限重询次数
    ACK_POLL_BACKOFF_S = 0.05   # 重询间仿真退避

    def __init__(self) -> None:
        self.words: dict[str, str] = {}

    def issue(self, op_id: str) -> None:
        self.words[op_id] = "BUSY"

    def finish(self, op_id: str) -> None:
        """设备内: 动作程序到点 → 状态字置位 (模拟伺服 in-position)。"""
        if self.words.get(op_id) == "BUSY":
            self.words[op_id] = "DONE"

    def ack(self, op_id: str) -> bool:
        return self.words.get(op_id) == "DONE"

    def poll(self, op_id: str, advance_s: Callable[[float], None]) -> bool:
        """有限重询: 至多 ACK_POLL_MAX 次, 间隔退避推进仿真时钟。"""
        for i in range(self.ACK_POLL_MAX):
            if self.ack(op_id):
                return True
            advance_s(self.ACK_POLL_BACKOFF_S * (i + 1))
        return self.ack(op_id)
