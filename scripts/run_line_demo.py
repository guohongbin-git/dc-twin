"""line-feed P0' 演示: 30min 仿真时钟 takt 拉料连续运转 (蓝图 §13)。

用法: PYTHONPATH=<core>:<dc-twin> python3 scripts/run_line_demo.py
产物: reports/line/p0_demo/{truth,line_ledger,evidence_events}.json
      + 评分器对账报告 (stdout)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dc_twin.line.runtime import run_line          # noqa: E402
from dc_twin.scorer.line import score_line_run     # noqa: E402


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "reports" / "line" / "p0_demo"
    # 暂存容量 10 (携托凸台不可跨位, 见 scene.staging_slot): 闭环
    # 连续运转 10 拍 ~8min; 30min 需更高举升行程或二次供料, 见边界。
    run = run_line(faults=frozenset(), out_dir=out, horizon_s=540.0,
                   takt_s=45.0, t0_s=12.0, line_pallets=12,
                   run_id="LINE-P0-DEMO")
    truth = run.truth
    print(f"run_id={run.run_id} out={run.out_dir}")
    print(f"cycles={len(truth['cycles'])} "
          f"finished={len(truth['finished'])} "
          f"stoppage={truth['stoppage_ticks']} "
          f"max_pen={truth['audit']['max_pen_m']*1000:.2f}mm")
    rep = score_line_run(run.out_dir)
    print(f"scorer: reconciled={rep.reconciled} "
          f"physical={rep.physical_completions} "
          f"ledger={rep.ledger_completions} "
          f"zero_stoppage={rep.zero_stoppage}")
    print("verdicts:", rep.summary)


if __name__ == "__main__":
    main()
