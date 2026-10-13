"""dc-twin: 证据事务驱动的配送中心数字孪生 (L3 场景载荷, 组合
agv-warehouse-core L1/L1.5 内核)。

分层 (蓝图 v0.5.1): core 不为场景硬编码 —— tk:* 节点 / load 腿 /
LOAD 事件类型 / 卡车时刻表全部经 core 注册表进主轴 (dc_twin.layout /
dc_twin.legs / dc_twin.evidence / dc_twin.timetable 的 register())。
"""
from __future__ import annotations

__version__ = "0.1.0"


def core_version() -> str:
    """被组合内核的版本 (评分器元数据; 打包态取 metadata, 工作仓取
    pyproject)。"""
    try:
        from importlib.metadata import version

        return version("agv-warehouse-core")
    except Exception:
        pass
    try:
        import re
        from pathlib import Path

        import sim

        pp = Path(sim.__file__).resolve().parent.parent / "pyproject.toml"
        m = re.search(r'^version\s*=\s*"([^"]+)"',
                      pp.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "unknown"
