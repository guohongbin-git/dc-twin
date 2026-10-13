"""渲染 P0 场景演示片: run_p0 离屏出帧 → ffmpeg mp4。"""
import subprocess, sys
from pathlib import Path
sys.path.insert(0, "/Users/guohongbin/1-Projects/desktop-agv-demo")
sys.path.insert(0, "/Users/guohongbin/1-Projects/dc-twin")
from dc_twin.runtime import run_p0

out = Path("/tmp/dc_p0_frames"); out.mkdir(exist_ok=True)
result = run_p0(render_dir=str(out), render_every=0.25)
print("闭环数:", result.loaded_departed_closed,
      "| 非原子窗口:", result.nonatomic_window_s, "s")
frames = sorted(out.glob("f_*.png"))
print("frames:", len(frames))
lst = out / "list.txt"
lst.write_text("\n".join(f"file '{f}'" for f in frames))
mp4 = Path("/Users/guohongbin/1-Projects/dc-twin/deliverables/dc_p0_demo.mp4")
mp4.parent.mkdir(exist_ok=True)
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", str(lst), "-vf", "scale=1280:720",
                "-r", "8", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                "-crf", "23", str(mp4)], check=True)
print("成片:", mp4)
