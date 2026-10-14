# 场景进化门 (gate)

实现: `dc_twin/esr/gate.py`。蓝图出处: §17.2 L+3 (消融子集指标预注册)
+ §18.③ ("场景进化门: 新注册类型必须附 (证据契约草案 + 消融子集) 方可
入册", 实现追加第三件: 回归用例)。

分层设计:

- `validate_proposal` / `review`: **纯函数校验器** (无 core 依赖, 错误
  清单可直接回喂 LLM 草稿回路);
- `admit`: 审查通过后经 core 既有注册表接口**落册** (懒导入; 注册器可
  注入供测试/宿主替换); core 拒绝 (重名/非法名) → `GateError`。

## 提案模型 EvolutionProposal

```python
from dc_twin.esr.gate import (EvidenceContractDraft, AblationSubset,
                              EvolutionProposal, GateVerdict, GateError,
                              validate_proposal, review, admit)

proposal = EvolutionProposal(
    proposal_id="PROP-INC-…",          # 非空字符串
    kind="event_kind",                 # "event_kind" | "node_source"
    payload={"kind": "LANE_INTERLOCK",       # event_kind: {kind, description}
             "description": "行线道岔联锁 …"},# node_source: {fn, name?}
    evidence_contract=…, ablation=…, regression_cases=…)
```

## 准入三件套 (缺一拒绝)

### ① 证据契约草案 EvidenceContractDraft

| 字段 | 要求 |
|---|---|
| `name` | 非空 (如 `esr.contract.inc-detfix-m2-001/0.1`) |
| `event_kinds` | 非空 —— 契约必须声明**哪些事件类型能证明哪些状态** (§17.2 L+1) |
| `guarantees` | 非空 —— 契约保证 (提交语义/恢复行为/窗口上界) |
| `status` | 恒 `"draft"` —— 门只收草案, 定稿由 ⑤ 消融门推动 |

### ② 消融子集定义 AblationSubset

| 字段 | 要求 |
|---|---|
| `name` | 非空 |
| `base_subset` | 非空, 须引用既有消融基线 (如 `"B0-B4"`) |
| `scenarios` | 非空 (通常取回归用例 scenario 并集) |
| `seeds` | 非空且全为整数 (bool 不算) |
| `metrics` | 非空 —— **指标进门时冻结 (预注册)**, 对齐 §17.2 L+3 |

### ③ 回归用例 (esr.crystallize.RegressionCase)

非空; 每条 `case_id`/`scenario` 非空、`asserts` 非空且算子合法
(沿用结晶格式的 `ASSERT_OPS`)。

### 载荷形状前置纯校验 (core 终审仍在落册时)

- `event_kind`: `payload["kind"]` 必须匹配 core 事件类型命名规则
  `^[A-Z][A-Z0-9_]*$` (大写字母/数字/下划线, 字母开头);
- `node_source`: `payload["fn"]` 必须可调用 (持久化工件以
  `"pkg.module:attr"` 字符串携带, `dc_twin.esr.loop._import_fn` 在进门
  时才解析)。

## API 与 GateError 语义

```python
validate_proposal(p) -> list[str]   # 纯函数: 错误清单 (空 = 放行)
review(p)           -> GateVerdict  # 门审查, 不落册
admit(p, *, node_source_registrar=None, event_kind_registrar=None,
      idempotent=False) -> GateVerdict   # 审查 + 落册
```

- **审查不过不注册**: `admit` 先 `review`, 有错误原样返回裁决
  (`approved=False`, `rejections` 逐条列出)。
- **GateError = 审查通过但落册失败** (`RuntimeError` 子类): core 对
  重名/非法名抛的 `ValueError`/`KeyError`/`TypeError` 被包装为
  `GateError("场景进化门落册失败 (PROP-…): …")` 重抛。即: 提案没过
  校验不会拿到 GateError (那是 `rejections`); 过了校验但注册表拒绝
  才是 GateError。
- **GateVerdict**: `{proposal_id, approved, rejections: tuple[str,…],
  registered: tuple[str,…]}`; `registered` 条目形如
  `event_kind:LANE_INTERLOCK` / `node_source:conv_nodes`, 幂等放行带
  `@idempotent` 后缀。
- 缺省 core 注册表接口 (懒导入): 事件类型 =
  `services.world.evidence.register_event_kind` (+ 只读 `EVENT_KINDS`);
  节点源 = `sim.warehouse_cell.register_node_source`。打包态装
  `agv-warehouse-core` 即可, 工作仓 tests 以 sys.path 注入。

## 幂等重注册 (idempotent=True)

自进化闭环同进程多轮: 同一提案复跑 (Loop ④ 拍即
`admit(proposal, idempotent=True)`)。`idempotent=True` **仅限缺省 core
路径** (注入注册器时忽略):

- `event_kind` 已在册且**描述相同** → 幂等放行,
  `registered=("event_kind:KIND@idempotent",)`;
- 已在册但**描述漂移** → `GateError` (描述不一致必须显式处理, 不许
  静默覆盖);
- `node_source` 为**同一函数对象**已在册 → 幂等放行; 异源重名仍走
  core 拒绝。

core 单调语义不绕过 —— 幂等只是"同源重注册放行", 不是改写。

## Loop ④ 拍的装配

`ESRLoop._proposal_from(record)` 从结晶记录自动装配:

- 注册载荷取契约修订 `proposed_changes` 中**第一条**带 `register` 的
  指令 (`as` 决定 kind);
- 契约草案: `name=f"esr.contract.{incident_id.lower()}/0.1"`,
  `event_kinds` 取 register 里的 event_kind (无则 `"N/A"`),
  `guarantees` 取各 change 的 `change` 文本;
- 消融子集: `base_subset="B0-B4"`, `scenarios`/`seeds`/`metrics` 由
  回归用例聚合 (scenario 并集 / seed 并集 / assert metric 并集 ——
  即指标预注册);
- 回归用例原样携带。

多条注册指令 (如 conveyor 的 node_source + 两条 event_kind) 在 Loop ④
只落第一条, 其余按同门逐条补册 —— 范式见
`scripts/esr_conveyor_walkthrough.py`。
