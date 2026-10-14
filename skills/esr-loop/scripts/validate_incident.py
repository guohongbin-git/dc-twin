#!/usr/bin/env python3
"""校验 ESR 技能结晶四件套 (esr.incident_record/0.1) —— 薄封装。

校验逻辑全部来自 dc_twin.esr.crystallize (纯函数, 无 core 依赖):
schema 错误 + 语义门 (动作必有回归保护 / covers_action 指向存在 /
断言算子合法 / 契约修订恒 draft)。本脚本只做: 定位 dc_twin 包 +
读文件 + 调校验器 + 报告。

用法:
    python3 validate_incident.py <incident_dir|incident.json> [--json]

定位 dc_twin 包 (依次尝试):
    1. 环境变量 DC_TWIN_HOME 指向 dc-twin 仓库根;
    2. dc_twin 已可导入 (PYTHONPATH / 已安装);
    3. 从当前目录逐级向上找含 dc_twin/esr/crystallize.py 的仓库根。

退出码: 0 = 校验通过; 1 = 校验未过 (错误清单见输出); 2 = 用法/定位错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCHEMA_ID = "esr.incident_record/0.1"


def _locate_dc_twin() -> None:
    """把 dc-twin 仓库根注入 sys.path (见模块 docstring 三级策略)。"""
    try:
        import dc_twin.esr.crystallize  # noqa: F401
        return
    except ImportError:
        pass
    roots: list[Path] = []
    home = os.environ.get("DC_TWIN_HOME")
    if home:
        roots.append(Path(home).expanduser().resolve())
    here = Path(__file__).resolve()
    roots.extend(here.parents)          # skill 装在仓库/安装树内时自动发现
    cwd = Path.cwd().resolve()
    roots.extend(cwd.parents)
    roots.append(cwd)
    for root in roots:
        if (root / "dc_twin" / "esr" / "crystallize.py").is_file():
            sys.path.insert(0, str(root))
            return
    sys.stderr.write(
        "错误: 找不到 dc_twin 包 (ESR 校验器依赖 dc-twin 仓库)。\n"
        "任选其一:\n"
        "  1. export DC_TWIN_HOME=/path/to/dc-twin\n"
        "  2. export PYTHONPATH=/path/to/dc-twin:$PYTHONPATH\n"
        "  3. 在 dc-twin 仓库内运行本脚本\n")
    raise SystemExit(2)


def _usage_error(message: str) -> SystemExit:
    sys.stderr.write(message + "\n")
    return SystemExit(2)


def load_incident(target: Path) -> tuple[dict, Path]:
    """读取 incident.json (接受事故目录或直接指向 incident.json)。"""
    path = target / "incident.json" if target.is_dir() else target
    if not path.is_file():
        raise _usage_error(f"错误: 找不到 {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except json.JSONDecodeError as exc:
        raise _usage_error(f"错误: {path} 不是合法 JSON: {exc}") from exc


def validate(obj: dict) -> list[str]:
    """schema 版本检查 + validate_semantics (错误清单, 空 = 通过)。"""
    from dc_twin.esr.crystallize import validate_semantics

    errs: list[str] = []
    if obj.get("schema") != SCHEMA_ID:
        errs.append(f"/schema: 版本不支持 {obj.get('schema')!r} "
                    f"(需 {SCHEMA_ID!r})")
    return errs + validate_semantics(obj)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 ESR 技能结晶四件套 (dc_twin.esr.crystallize "
                    "薄封装)")
    parser.add_argument("target", type=Path,
                        help="事故目录 (含 incident.json) 或直接指向 "
                             "incident.json")
    parser.add_argument("--json", action="store_true",
                        help="机器可读输出 (单行 JSON)")
    args = parser.parse_args(argv)

    _locate_dc_twin()
    obj, path = load_incident(args.target)
    errs = validate(obj)
    record = None
    if not errs and path.name == "incident.json":
        # 定档确认 (纯函数; 仅当确实指向 incident.json 时可定位事故目录)
        from dc_twin.esr.crystallize import crystallize
        record = crystallize(path.parent)

    if args.json:
        print(json.dumps(
            {"ok": not errs, "target": str(args.target),
             "schema": obj.get("schema"), "errors": errs,
             "incident_id": (record.incident_id if record else None)},
            ensure_ascii=False))
    else:
        label = path
        if errs:
            print(f"✗ {label}: 校验未过 ({len(errs)} 项)")
            for e in errs:
                print(f"  - {e}")
        else:
            print(f"✓ {label}: PASS (schema + 语义门)")
            if record is not None:
                print(f"  incident_id : {record.incident_id} — {record.title}")
                print(f"  修复动作     : {len(record.fix_actions)} 条 "
                      f"{[a.action_id for a in record.fix_actions]}")
                print(f"  回归用例     : {len(record.regression_cases)} 条 "
                      f"(覆盖 "
                      f"{sorted({c.covers_action for c in record.regression_cases})})")
                print(f"  契约修订     : {record.contract.revision_kind} / "
                      f"{record.contract.status}")
    return 0 if not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())
