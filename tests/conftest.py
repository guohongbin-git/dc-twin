"""dc-twin 测试引导: 复用 core 工作仓 (sys.path 注入, 蓝图 v0.5.1 §6
dc-twin 锁 core minor; 打包态下安装 agv-warehouse-core 即可, 工作仓
优先以便联动调试)。"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path("~/1-Projects/desktop-agv-demo").expanduser().resolve()
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
