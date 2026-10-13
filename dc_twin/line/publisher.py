"""产线状态发布器 (蓝图 §15 面板 tab 产线 的文件契约数据源)。

run_line 循环内每 interval_s (仿真秒) 调 ``maybe_publish`` 把
``_LiveState.snapshot()`` 原子写 (tmp+``os.replace``) 到状态 JSON;
matrix_server (:8400) 的 ``/api/line/state`` 读同一文件 (无文件 →
``{"available": false}``, 面板显示产线离线)。纯文件契约组合,
不 import 对方模块。

路径解析: ``run_line(state_path=...)`` 显式参数 → 环境变量
``DC_TWIN_LINE_STATE_PATH`` → ``DEFAULT_LINE_STATE_PATH``;
``state_path=False`` 关闭发布 (测试隔离用)。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

#: 默认发布路径 (面板侧 matrix_server 默认读同一处; 保持 ~ 未展开
#: 以便常量断言与跨机可移植, 写入时 expanduser)。
DEFAULT_LINE_STATE_PATH = Path("~/1-Projects/dc-twin/reports/line_state.json")
ENV_LINE_STATE_PATH = "DC_TWIN_LINE_STATE_PATH"


def resolve_state_path(state_path=None) -> Path | None:
    """显式参数 → 环境变量 → 默认路径; False 显式关闭。"""
    if state_path is False:
        return None
    if state_path:
        return Path(state_path).expanduser()
    env = os.environ.get(ENV_LINE_STATE_PATH)
    if env:
        return Path(env).expanduser()
    return DEFAULT_LINE_STATE_PATH.expanduser()


class LineStatePublisher:
    """把活动状态快照周期性原子写成 JSON (面板唯一数据通道)。"""

    def __init__(self, path, live, *, run_id: str, interval_s: float = 1.0):
        self.path = Path(path).expanduser()
        self._live = live
        self._run_id = run_id
        self.interval_s = float(interval_s)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_t: float | None = None
        self.writes = 0

    def maybe_publish(self, sim_t: float) -> bool:
        """首拍必写, 之后每 interval_s (仿真秒) 至多一写。"""
        if self._last_t is not None and sim_t - self._last_t < self.interval_s:
            return False
        self.publish()
        self._last_t = float(sim_t)
        return True

    def publish(self) -> None:
        payload = {
            "available": True,
            "run_id": self._run_id,
            "written_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "core_version": _core_version(),
            "state": self._live.snapshot(),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)
        self.writes += 1


def _core_version() -> str:
    from .. import core_version
    return core_version()
