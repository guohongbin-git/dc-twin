"""技能结晶四件套格式 (蓝图 §18.3): incident → {根因假设, 修复动作,
回归用例, 证据契约修订}。

DetFix 是首个样本 (蓝图 §18.②: 16 runs 把 6 层故障链炼成三修复;
本包 fixture tests/fixtures/esr/detfix_m2 即其取证移植)。格式由
schemas/incident_record.schema.json 权威定义 (JSON Schema 2020-12
子集, 内置校验器按同一子集执行, 不引入第三方依赖); 语义校验
(validate_semantics, 纯函数) 在 schema 之上追加四件套门:

- 每条修复动作必须有回归用例保护 (缺一拒绝);
- 回归用例 covers_action 必须指向存在的 action_id;
- 证据契约修订永远以 draft 状态出场 —— 修订是否入册由场景进化门
  (esr.gate) 裁决, 结晶层只起草。

LLM 用法: ②拍 LLM 草稿即一份候选 dict, 经 validate_semantics 迭代
修复后由 crystallize() 定档 —— 校验器返回错误清单而非抛异常, 就是
为草稿回路设计的。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_ID = "esr.incident_record/0.1"

#: 断言算子 → 是否要求随附 value (evaluate_assert 同源, gate/loop 复用);
#: 符号形 (<=, >=, <, >, ==, !=) 为字词形的合法别名
ASSERT_OPS: dict[str, bool] = {
    "eq": True, "ne": True, "lt": True, "le": True,
    "gt": True, "ge": True, "contains": True,
    "empty": False, "nonempty": False,
    "==": True, "!=": True, "<": True, "<=": True, ">": True, ">=": True,
}

_OP_ALIAS = {"==": "eq", "!=": "ne", "<": "lt",
             "<=": "le", ">": "gt", ">=": "ge"}

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "incident_record.schema.json"


# ================================================================ 数据模型
@dataclass(frozen=True)
class RootCauseHypothesis:
    """根因假设: 陈述 + 机制链 + 置信度 + 取证引用。"""

    statement: str
    confidence: float
    mechanism: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class FixAction:
    """修复动作: 作用对象 + 做法 + 预期效果。"""

    action_id: str
    target: str
    description: str
    expected_effect: str = ""


@dataclass(frozen=True)
class RegressionCase:
    """回归用例: 场景/种子 + 结构化断言 + 所保护的修复动作。

    asserts 为结构化判据 ({"metric","op","value"?}), 由
    evaluate_assert 求值 —— 不 eval 任意表达式。evidence 是相对
    incident_dir 的运行工件路径 (fixture 或真实 run 导出)。
    """

    case_id: str
    scenario: str
    asserts: tuple[dict, ...]
    covers_action: str
    name: str = ""
    seeds: tuple[int, ...] = ()
    evidence: str = ""


@dataclass(frozen=True)
class EvidenceContractRevision:
    """证据契约修订草案: 状态恒为 draft, 入册由场景进化门裁决。

    proposed_changes 条目可携带 register 指令
    ({"as": "event_kind"|"node_source", ...}) —— ④拍据此组装注册
    载荷, 经 core register_event_kind / register_node_source 落册。
    """

    revision_kind: str                      # none | amend | new
    status: str = "draft"
    summary: str = ""
    proposed_changes: tuple[dict, ...] = ()


@dataclass(frozen=True)
class IncidentRecord:
    """一次事故的技能结晶定档 (四件套齐备, JSON 可持久化)。"""

    incident_id: str
    title: str
    root_cause: RootCauseHypothesis
    fix_actions: tuple[FixAction, ...]
    regression_cases: tuple[RegressionCase, ...]
    contract: EvidenceContractRevision
    schema: str = SCHEMA_ID
    window: dict = field(default_factory=dict)
    runs: tuple[str, ...] = ()
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "incident_id": self.incident_id,
            "title": self.title,
            "window": dict(self.window),
            "runs": list(self.runs),
            "source": self.source,
            "root_cause_hypothesis": {
                "statement": self.root_cause.statement,
                "mechanism": list(self.root_cause.mechanism),
                "confidence": self.root_cause.confidence,
                "evidence_refs": list(self.root_cause.evidence_refs),
            },
            "fix_actions": [
                {"action_id": a.action_id, "target": a.target,
                 "description": a.description,
                 "expected_effect": a.expected_effect}
                for a in self.fix_actions],
            "regression_cases": [
                {"case_id": c.case_id, "name": c.name,
                 "seeds": list(c.seeds), "scenario": c.scenario,
                 "asserts": [dict(a) for a in c.asserts],
                 "covers_action": c.covers_action, "evidence": c.evidence}
                for c in self.regression_cases],
            "evidence_contract_revision": {
                "revision_kind": self.contract.revision_kind,
                "summary": self.contract.summary,
                "proposed_changes": [dict(c) for c in
                                     self.contract.proposed_changes],
                "status": self.contract.status,
            },
        }

    @classmethod
    def from_dict(cls, obj: dict) -> "IncidentRecord":
        rc = obj["root_cause_hypothesis"]
        return cls(
            incident_id=obj["incident_id"], title=obj["title"],
            schema=obj.get("schema", SCHEMA_ID),
            window=dict(obj.get("window", {})),
            runs=tuple(obj.get("runs", ())),
            source=obj.get("source", ""),
            root_cause=RootCauseHypothesis(
                statement=rc["statement"], confidence=rc["confidence"],
                mechanism=tuple(rc.get("mechanism", ())),
                evidence_refs=tuple(rc.get("evidence_refs", ()))),
            fix_actions=tuple(
                FixAction(a["action_id"], a["target"], a["description"],
                          a.get("expected_effect", ""))
                for a in obj["fix_actions"]),
            regression_cases=tuple(
                RegressionCase(
                    case_id=c["case_id"], scenario=c["scenario"],
                    asserts=tuple(dict(a) for a in c["asserts"]),
                    covers_action=c["covers_action"],
                    name=c.get("name", ""),
                    seeds=tuple(c.get("seeds", ())),
                    evidence=c.get("evidence", ""))
                for c in obj["regression_cases"]),
            contract=EvidenceContractRevision(
                revision_kind=obj["evidence_contract_revision"]
                ["revision_kind"],
                status=obj["evidence_contract_revision"].get(
                    "status", "draft"),
                summary=obj["evidence_contract_revision"].get("summary", ""),
                proposed_changes=tuple(
                    dict(c) for c in obj["evidence_contract_revision"]
                    .get("proposed_changes", ()))),
        )


# ================================================================ 校验
def _type_ok(val, typ: str) -> bool:
    if typ == "object":
        return isinstance(val, dict)
    if typ == "array":
        return isinstance(val, list)
    if typ == "string":
        return isinstance(val, str)
    if typ == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if typ == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if typ == "boolean":
        return isinstance(val, bool)
    if typ == "null":
        return val is None
    return False


def _schema_errors(val, schema: dict, path: str = "") -> list[str]:
    """内置 JSON Schema 校验器 (2020-12 子集, 与随包 schema 同源)。"""
    errs: list[str] = []
    where = path or "/"
    if "const" in schema and val != schema["const"]:
        errs.append(f"{where}: const 应为 {schema['const']!r}, 实得 "
                    f"{val!r}")
        return errs
    if "enum" in schema and val not in schema["enum"]:
        errs.append(f"{where}: {val!r} 不在枚举 {schema['enum']}")
        return errs
    if "type" in schema and not _type_ok(val, schema["type"]):
        errs.append(f"{where}: 类型应为 {schema['type']}, 实得 "
                    f"{type(val).__name__}")
        return errs
    if isinstance(val, str):
        if "minLength" in schema and len(val) < schema["minLength"]:
            errs.append(f"{where}: 长度 {len(val)} < minLength "
                        f"{schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], val):
            errs.append(f"{where}: {val!r} 不匹配 {schema['pattern']}")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if "minimum" in schema and val < schema["minimum"]:
            errs.append(f"{where}: {val} < minimum {schema['minimum']}")
        if "maximum" in schema and val > schema["maximum"]:
            errs.append(f"{where}: {val} > maximum {schema['maximum']}")
    if isinstance(val, dict):
        for key in schema.get("required", ()):
            if key not in val:
                errs.append(f"{where}: 缺必填字段 {key!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in val:
                if key not in props:
                    errs.append(f"{where}: 多余字段 {key!r}")
        for key, sub in props.items():
            if key in val:
                errs += _schema_errors(val[key], sub,
                                       f"{path}/{key}" if path
                                       else f"/{key}")
    if isinstance(val, list):
        if "minItems" in schema and len(val) < schema["minItems"]:
            errs.append(f"{where}: {len(val)} 项 < minItems "
                        f"{schema['minItems']}")
        if "items" in schema:
            for i, item in enumerate(val):
                errs += _schema_errors(item, schema["items"],
                                       f"{path}/{i}" if path
                                       else f"/{i}")
    return errs


def load_schema() -> dict:
    """随包 JSON Schema (四件套格式的权威定义)。"""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_semantics(obj: dict) -> list[str]:
    """四件套校验 (纯函数): schema 错误 + 语义门, 返回错误清单。

    语义门 (schema 之上):
    - 每条修复动作必须有 ≥1 条回归用例保护;
    - covers_action 必须指向存在的 action_id;
    - 断言算子合法且带值算子必须随附 value;
    - 证据契约修订状态必须为 draft (入册交给 gate)。
    对残缺输入防御: 不抛异常, 只报错。
    """
    errs = _schema_errors(obj, load_schema())
    fixes = obj.get("fix_actions") or []
    cases = obj.get("regression_cases") or []
    fix_ids = [f.get("action_id") for f in fixes
               if isinstance(f, dict)]
    covered: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            continue
        target = case.get("covers_action")
        if target not in fix_ids:
            errs.append(f"/regression_cases: covers_action {target!r} "
                        f"不指向任何修复动作 (现有 {fix_ids})")
        else:
            covered.add(target)
        for spec in case.get("asserts") or []:
            if not isinstance(spec, dict):
                continue
            op = spec.get("op")
            if op not in ASSERT_OPS:
                errs.append(f"/regression_cases/{case.get('case_id')}: "
                            f"未知断言算子 {op!r}")
            elif ASSERT_OPS[op] and "value" not in spec:
                errs.append(f"/regression_cases/{case.get('case_id')}: "
                            f"算子 {op!r} 必须随附 value")
    for fid in fix_ids:
        if fid not in covered:
            errs.append(f"/fix_actions: 修复动作 {fid!r} 无回归用例保护 "
                        f"(四件套门: 缺一拒绝)")
    contract = obj.get("evidence_contract_revision")
    if isinstance(contract, dict) and contract.get("status") != "draft":
        errs.append("/evidence_contract_revision: 修订状态必须为 draft "
                    "(入册由场景进化门裁决)")
    return errs


# ================================================================ 解析
def crystallize(incident_dir: str | Path) -> IncidentRecord:
    """解析 incident_dir/incident.json → IncidentRecord (定档)。

    schema/语义任一不过 → ValueError (错误清单随异常文本)。
    """
    incident_dir = Path(incident_dir)
    obj = json.loads((incident_dir / "incident.json").read_text(
        encoding="utf-8"))
    if obj.get("schema") != SCHEMA_ID:
        raise ValueError(
            f"schema 版本不支持: {obj.get('schema')!r} (需 {SCHEMA_ID!r})")
    errs = validate_semantics(obj)
    if errs:
        raise ValueError("技能结晶校验未过:\n" + "\n".join(errs))
    return IncidentRecord.from_dict(obj)


# ================================================================ 断言求值
def _lookup(artifact: dict, dotted: str):
    cur: object = artifact
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def evaluate_assert(artifact: dict, spec: dict) -> tuple[bool, str]:
    """对运行工件求值一条结构化断言 → (是否通过, 细节)。

    缺指标/未知算子按失败处理 (回归门宁严勿漏), 不抛异常。
    """
    metric, op = spec.get("metric"), _OP_ALIAS.get(spec.get("op"),
                                                    spec.get("op"))
    val, found = _lookup(artifact, metric or "")
    if not found:
        return False, f"指标缺失: {metric!r}"
    try:
        if op == "empty":
            return (val is None or len(val) == 0), f"{metric}={val!r}"
        if op == "nonempty":
            return (val is not None and len(val) > 0), f"{metric}={val!r}"
        if op == "contains":
            want = spec.get("value")
            return (want in val), f"{metric} 含 {want!r}? {val!r}"
        if op in ("eq", "ne", "lt", "le", "gt", "ge"):
            want = spec.get("value")
            ok = {"eq": val == want, "ne": val != want,
                  "lt": val < want, "le": val <= want,
                  "gt": val > want, "ge": val >= want}[op]
            return bool(ok), f"{metric}={val!r} {op} {want!r}"
    except TypeError:
        return False, f"{metric}={val!r} 与 {spec.get('value')!r} 不可比"
    return False, f"未知断言算子: {op!r}"
