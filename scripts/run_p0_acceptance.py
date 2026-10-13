#!/usr/bin/env python
"""P0 验收驱动: 正向闭环 + 六类负向注入 → 独立评分器判定输出。

用法 (复用 core venv):
    cd ~/1-Projects/dc-twin
    PYTHONPATH=~/1-Projects/desktop-agv-demo \\
        ../desktop-agv-demo/.venv/bin/python scripts/run_p0_acceptance.py

输出: reports/p0/<run_id>/ (truth/ledger/evidence_events) + 判定摘要。
验收口径 (蓝图 v0.5.1 §3-P0 / §11 C2):
- 正向: ≥1 卡车装车离场且账实三元等式成立; 非原子窗口上界入报告;
- 六负向: 评分器必须识别; 正确但未闭合事务判「待恢复」。
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dc_twin.runtime import FAULTS, run_p0            # noqa: E402
from dc_twin.scorer import score_run                  # noqa: E402

NEGATIVE_EXPECT = {
    "forge_commit": "MISMATCH_FORGED_SUCCESS",
    "wrong_truck": "MISMATCH_WRONG_TRUCK",
    "crash_before_commit": "PENDING_RECOVERY",
    "ack_lost": "CONSISTENT",
    "duplicate_event": "CONSISTENT",
    "depart_unsettled": "MISMATCH_DEPARTURE_UNSETTLED",
}


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "reports" / "p0"
    root.mkdir(parents=True, exist_ok=True)
    ok = True
    rows: list[dict] = []

    # ---- 正向闭环 ----
    run_id = f"P0-{uuid.uuid4().hex[:8]}"
    out = root / run_id
    res = run_p0(faults=frozenset(), out_dir=out, run_id=run_id)
    rep = score_run(out)
    passed = (rep.loaded_departed_closed >= 1
              and all(o.verdict == "CONSISTENT" for o in rep.ops))
    ok &= passed
    rows.append({"case": "HAPPY", "expect": "CONSISTENT+≥1装车离场",
                 "got": [o.verdict for o in rep.ops],
                 "ldc": rep.loaded_departed_closed, "pass": passed,
                 "window_s": rep.max_nonatomic_window_s, "dir": str(out)})

    # ---- 六类负向 ----
    for fault in sorted(FAULTS):
        run_id = f"P0-{fault}-{uuid.uuid4().hex[:6]}"
        out = root / run_id
        run_p0(faults=frozenset({fault}), out_dir=out, run_id=run_id)
        rep = score_run(out)
        got = rep.ops[0].verdict
        expect = NEGATIVE_EXPECT[fault]
        passed = got == expect
        ok &= passed
        rows.append({"case": fault, "expect": expect, "got": got,
                     "anomalies": list(rep.ops[0].anomalies),
                     "pass": passed, "dir": str(out)})

    print(json.dumps({"all_pass": ok, "cases": rows}, ensure_ascii=False,
                     indent=1))
    (root / "acceptance_latest.json").write_text(
        json.dumps({"all_pass": ok, "cases": rows}, ensure_ascii=False,
                   indent=1) + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
