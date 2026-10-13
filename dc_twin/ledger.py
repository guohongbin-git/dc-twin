"""装运台账 (truck 维度) —— dc-twin 场景侧持久化事务日志。

段语义 (对齐 core 七段事务的裁剪版): prepare → (证据) → commit → ack。
- events = 传输/审计日志: 原始请求照实追加 (含重复请求), 物化状态
  幂等 (重复提交抑制并留痕) —— 评分器据日志识别重复事件 (负向 #5);
- snapshots = 冻结状态快照序列 (评分器快照边界判定的输入)。
"""
from __future__ import annotations


class ShipmentLedger:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.snapshots: list[dict] = []
        self._ops: dict[str, dict] = {}

    # ------------------------------------------------------------ 事务
    def prepare(self, operation_id: str, *, pallet_id: str, truck_id: str,
                tk_node: str, dock_node: str, t_s: float) -> None:
        if operation_id in self._ops:
            raise ValueError(f"装运单重复: {operation_id}")
        self._ops[operation_id] = {
            "state": "PREPARED", "pallet_id": pallet_id,
            "truck_id": truck_id, "tk_node": tk_node, "dock_node": dock_node,
            "prepare_t_s": float(t_s), "commit_t_s": None, "ack_t_s": None,
            "evidence_ref": None,
        }
        self.events.append({"type": "LoadPrepared",
                            "operation_id": operation_id, "ts_s": float(t_s),
                            "pallet_id": pallet_id, "truck_id": truck_id,
                            "tk_node": tk_node, "dock_node": dock_node})

    def commit(self, operation_id: str, *, t_s: float, evidence_ref: str,
               truck_id: str | None = None,
               _duplicate: bool = False) -> None:
        """提交装车事务; _duplicate=True 模拟重复请求 (照录+抑制)。"""
        op = self._ops.get(operation_id)
        if op is None:
            raise KeyError(f"未知装运单: {operation_id}")
        self.events.append({"type": "LoadCommitRequested",
                            "operation_id": operation_id, "ts_s": float(t_s),
                            "evidence_ref": evidence_ref,
                            "truck_id": truck_id or op["truck_id"]})
        if _duplicate:
            self.events.append({"type": "DuplicateSuppressed",
                                "operation_id": operation_id,
                                "ts_s": float(t_s),
                                "of": "LoadCommitRequested"})
            return
        if op["state"] in ("COMMITTED", "ACKED"):
            raise ValueError(f"装运单已提交 (幂等): {operation_id}")
        if truck_id is not None:
            op["truck_id"] = truck_id          # 场景注入点 (wrong_truck 用例)
        op["state"] = "COMMITTED"
        op["commit_t_s"] = float(t_s)
        op["evidence_ref"] = evidence_ref
        self.events.append({"type": "LoadCommitted",
                            "operation_id": operation_id, "ts_s": float(t_s),
                            "evidence_ref": evidence_ref,
                            "truck_id": op["truck_id"]})

    def ack(self, operation_id: str, *, t_s: float) -> None:
        op = self._ops.get(operation_id)
        if op is None:
            raise KeyError(f"未知装运单: {operation_id}")
        if op["state"] != "COMMITTED":
            raise ValueError(f"状态 {op['state']} ≠ COMMITTED, 不得 ack")
        op["state"] = "ACKED"
        op["ack_t_s"] = float(t_s)
        self.events.append({"type": "LoadAcked", "operation_id": operation_id,
                            "ts_s": float(t_s)})

    # ------------------------------------------------------------ 视图
    def state_of(self, operation_id: str) -> dict:
        return dict(self._ops[operation_id])

    def snapshot(self, *, t_s: float) -> dict:
        snap = {"t_s": float(t_s),
                "ops": {k: dict(sorted(v.items()))
                        for k, v in sorted(self._ops.items())}}
        self.snapshots.append(snap)
        return snap

    def export(self, *, run_id: str) -> dict:
        return {"run_id": run_id, "events": list(self.events),
                "snapshots": list(self.snapshots)}
