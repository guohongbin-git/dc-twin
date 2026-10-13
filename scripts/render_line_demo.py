"""渲染产线 P0' 场景演示片: LineCell 离屏出帧 (运镜三段 + 冲压循环)
→ ffmpeg mp4。

诚实标注: 渲染用真实物理场景 (MuJoCo), 供料闭环 (AGV 携托联动) 暂停,
冲压循环由 actuator 直驱; 尾帧已标注。
"""
import subprocess, sys
from pathlib import Path

sys.path.insert(0, "/Users/guohongbin/1-Projects/desktop-agv-demo")
sys.path.insert(0, "/Users/guohongbin/1-Projects/dc-twin")

import mujoco
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from sim.controllers import Ramp
from dc_twin.line.scene import (PRESS_Q_TARGET, LineCell, run_segments)

W, H = 1280, 720
FPS = 10
out = Path("/tmp/dc_line_frames")
out.mkdir(exist_ok=True)
for f in out.glob("f_*.png"):
    f.unlink()

lay = None
cell = None


def build():
    global cell
    from sim.warehouse_cell import MatrixLayout
    from dc_twin.line import runtime as rt

    lay = MatrixLayout(seed_pallets=0)
    lay.p["park"] = {**lay.p["park"], "A02": (*rt.IDLE_XY, 0.0),
                     "A03": (*rt.A03_IDLE_XY, 0.0)}
    cell = LineCell(layout=lay, line_pallets=6,
                    preload_racks=("L-B1", "L-B2"),
                    agvs=("A02", "A03"), forklifts=(), seed_slots=[])
    cell.warmup(0.5)
    # 演示预置: pl_000+wp_000 以 nest 直投末态 kinematic 播种 (诚实标注见尾帧)
    nest = cell.geos["ST-PRESS"].nest_y
    import mujoco as _m
    m, d, T = cell.model, cell.data, _m.mjtObj
    def seed(name, xy, z):
        bid = _m.mj_name2id(m, T.mjOBJ_BODY, name)
        jadr = m.jnt_qposadr[m.body_jntadr[bid]]
        d.qpos[jadr:jadr + 2] = xy
        d.qpos[jadr + 2] = z
        d.qvel[jadr:jadr + 6] = 0
    seed("pl_000", (2.50, 2.328), 0.076)
    seed("wp_000", (2.50, 2.328), 0.092)
    _m.mj_forward(m, d)
    cell.clamp("pl_000", True)
    return cell


cell = build()
h = cell.presses["ST-PRESS"]
v = cell.veh["A02"]
renderer = mujoco.Renderer(cell.model, height=H, width=W)
cam = mujoco.MjvCamera()
cam.type = mujoco.mjtCamera.mjCAMERA_FREE

font = None
try:
    font = ImageFont.load_default(size=44)
except TypeError:
    font = ImageFont.load_default()
font_s = None
try:
    font_s = ImageFont.load_default(size=26)
except TypeError:
    font_s = font


def title_frame(lines):
    img = Image.new("RGB", (W, H), "black")
    dr = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        bb = dr.textbbox((0, 0), line, font=font)
        w = bb[2] - bb[0]
        dr.text(((W - w) / 2, H / 2 - 60 + 70 * i), line,
                fill="white", font=font)
    return np.asarray(img)


def tail_frame(lines):
    img = Image.new("RGB", (W, H), "black")
    dr = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        bb = dr.textbbox((0, 0), line, font=font_s)
        w = bb[2] - bb[0]
        dr.text(((W - w) / 2, H / 2 - 40 + 56 * i), line,
                fill=(200, 200, 200), font=font_s)
    return np.asarray(img)


frames = []          # (np.ndarray,)
N = frames.append


def cam_to(lookat, distance, azimuth, elevation):
    cam.lookat[:] = lookat
    cam.distance = distance
    cam.azimuth = azimuth
    cam.elevation = elevation


def shot():
    renderer.update_scene(cell.data, camera=cam)
    N(renderer.render().copy())


def advance(seconds):
    for _ in range(int(seconds / cell.model.opt.timestep)):
        cell.step(1)


# ---------------- 片头 2s 黑底白字 ----------------
for _ in range(2 * FPS):
    N(title_frame(["Evidence-Grounded Bottling Line",
                   "dc-twin P0' · 产线供料场景"]))

# ---------------- 段1: 高机位缓降+环绕 8s (azimuth 150→90) --------
seg1, n1 = 8.0, 80
for i in range(n1):
    u = i / (n1 - 1)
    cam_to((2.55, 2.30, 0.12), 3.6 - 0.4 * u, 150 - 60 * u, -38 + 14 * u)
    shot()
    advance(seg1 / n1)

# ---------------- 段2: 推轨到工位正面 3s --------------------------
seg2, n2 = 3.0, 30
a0, d0, l0 = 90, 3.2, (2.55, 2.30, 0.12)
a1, d1, l1 = 180, 1.1, (2.50, 2.325, 0.075)
for i in range(n2):
    u = (i / (n2 - 1)) ** 0.6
    cam_to((l0[0] + (l1[0] - l0[0]) * u,
            l0[1] + (l1[1] - l0[1]) * u,
            l0[2] + (l1[2] - l0[2]) * u),
           d0 + (d1 - d0) * u, a0 + (a1 - a0) * u, -38 + 20 * u)
    shot()
    advance(seg2 / n2)

# ---------------- 段3: 工位特写 4 次冲压循环 22s -------------------
CYC, dwell = 4, 0.4
down, dwell_t, up = 1.6, 0.5, 1.6
per = down + dwell_t + up
cam_to((2.50, 1.95, 0.10), 1.0, 185, -12)
for cyc in range(CYC):
    q0 = h.q_press(cell)
    run_segments(cell, [(h.press_aid,
                         lambda: Ramp(q0, -0.043, 0.030, 0.10), 0.0, 0.0)])
    t0 = cell.data.time
    while cell.data.time - t0 < down + dwell_t:
        cam_to((2.50, 1.95, 0.09 + 0.012 * np.sin(cell.data.time * 3)),
               1.0, 185 + 4 * np.sin(cell.data.time), -12)
        shot()
        advance(1.0 / FPS)
    run_segments(cell, [(h.press_aid,
                         (lambda q0: (lambda: Ramp(q0, 0.0, 0.030, 0.10)))(
                             h.q_press(cell)), 0.0, 0.0)])
    t0 = cell.data.time
    while cell.data.time - t0 < up:
        shot()
        advance(1.0 / FPS)

# ---------------- 段4: 回全景 5s ----------------------------------
seg4, n4 = 5.0, 50
a0, d0 = 185, 1.0
for i in range(n4):
    u = (i / (n4 - 1)) ** 0.7
    cam_to((2.50 + (2.55 - 2.50) * u, 2.325 + (2.30 - 2.325) * u, 0.12),
           1.0 + (3.2 - 1.0) * u, 185 + (150 - 185) * u, -12 + (-24) * u)
    shot()
    advance(seg4 / n4)

# ---------------- 尾帧 3s 诚实标注 --------------------------------
for _ in range(3 * FPS):
    N(tail_frame(["物理保真以 MuJoCo 模型为准",
                  "携托联动 P0'.5 修复中 · 供料闭环演示暂停"]))

# ---------------- ffmpeg ------------------------------------------
pngs = []
for i, arr in enumerate(frames):
    p = out / f"f_{i:05d}.png"
    Image.fromarray(arr).save(p)
    pngs.append(p)
lst = out / "list.txt"
lst.write_text("\n".join(f"file '{f}'" for f in pngs))
mp4 = Path("/Users/guohongbin/1-Projects/dc-twin/deliverables/line_p0_demo.mp4")
mp4.parent.mkdir(exist_ok=True)
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", str(lst), "-vf", "scale=1280:720",
                "-r", str(FPS), "-pix_fmt", "yuv420p", "-c:v", "libx264",
                "-crf", "23", str(mp4)], check=True)
print("成片:", mp4, "frames:", len(pngs))
