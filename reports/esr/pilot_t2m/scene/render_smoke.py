#!/usr/bin/env python3
"""t2m_handoff_cell render smoke (text2mujoco skill 渲染层)。

在 独立进程 且带 GL 后端的环境运行 (Linux: MUJOCO_GL=egl, 失败换
osmesa; macOS: mjpython + MUJOCO_GL=glfw/cgl 需活动图形会话):

    MUJOCO_GL=egl python3 render_smoke.py            # Linux
    mjpython render_smoke.py                          # macOS (glfw/cgl)

校验: 分辨率/非空 RGB/有限深度/任务相关像素变化 (动作序列前后)。
产物: output/screenshots/{before,after}.png + after_depth.npy +
render_results.json。物理层证据见 physics_smoke (GL 无关)。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(_PKG))

import mujoco                                  # noqa: E402
import environment as E                        # noqa: E402

WIDTH, HEIGHT = 640, 480


def main() -> int:
    result = {
        "schema": "t2m.render_smoke/0.1",
        "scene": "t2m_handoff_cell",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mujoco_version": mujoco.__version__,
        "gl_backend": os.environ.get("MUJOCO_GL", "(unset)"),
    }
    shots = _PKG / "output" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)

    env = E.HandoffCellEnv()
    env.reset(seed=7)
    model, data = env.model, env.data

    renderer = mujoco.Renderer(model, height=HEIGHT, width=WIDTH)
    try:
        def capture(path_png, path_depth=None):
            renderer.update_scene(data, camera="handoff_cam")
            rgb = renderer.render().copy()
            from PIL import Image
            Image.fromarray(rgb).save(path_png)
            out = {"rgb_shape": list(rgb.shape),
                   "rgb_nonblank": bool(rgb.std() > 2.0),
                   "rgb_mean": float(rgb.mean())}
            if path_depth is not None:
                renderer.enable_depth_rendering()
                renderer.update_scene(data, camera="handoff_cam")
                depth = renderer.render().copy()
                renderer.disable_depth_rendering()
                np.save(path_depth, depth)
                out["depth_shape"] = list(depth.shape)
                out["depth_finite"] = bool(np.isfinite(depth).all())
                out["depth_range_m"] = [float(np.nanmin(depth)),
                                        float(np.nanmax(depth))]
            return rgb, out

        before_rgb, before = capture(shots / "before.png")
        result["before"] = before

        # 动作序列: 送箱 + 抓取 + 入箱 (渲染层采样同一物理序列)
        for pid in E.DEPENDENCY_ORDER:
            payload = {"close": True} if pid == "grip_close" else {}
            env.step({"id": pid, "payload": payload})

        after_rgb, after = capture(shots / "after.png",
                                   shots / "after_depth.npy")
        result["after"] = after

        moved = int(np.abs(after_rgb.astype(int)
                           - before_rgb.astype(int)).sum())
        result["pixel_change_sum"] = moved
        result["task_success"] = bool(env.is_success())
        checks = [before["rgb_nonblank"], after["rgb_nonblank"],
                  after.get("depth_finite", False), moved > 10_000,
                  result["task_success"]]
        result["status"] = "PASS" if all(checks) else "FAIL"
    except Exception as exc:                                  # noqa: BLE001
        result["status"] = "FAIL"
        # 只留错误类别, 不留本机路径/命令行
        result["error_category"] = type(exc).__name__
    finally:
        renderer.close()

    out = _PKG / "output" / "render_results.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps({k: result[k] for k in
                      ("status", "gl_backend", "pixel_change_sum")
                      if k in result}, ensure_ascii=False))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
