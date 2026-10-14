<p align="center">
  <img src="https://raw.githubusercontent.com/topprismdata/.github/main/assets/brand/topprism-repo-header.png" alt="TopPrism dual-prism visual" width="100%" />
</p>

# dc-twin

> **语言 / Language:** 中文为主 · English overview follows。

### ESR（Evidence-Simulation-Ready）产线数字孪生参考实现 —— 有智商的 SimReady 超集

`Purpose: NATIVE AI` · `Type: FRAMEWORK REFERENCE` · `Evidence: RUN-PROVEN LOOP`

> 属于 **TopPrism Native AI** — 让组织级仿真能力在运行经验中持续复利的工程体系。

**English overview:** dc-twin is the reference implementation of ESR, an
open, agentic, evidence-native alternative to NVIDIA SimReady. Scenes and
assets here are not static content: every run crystallizes experience into
skills, and every evolution must pass an ablation gate before it may rewrite
the specification.

---

## Why this exists

SimReady 定义了"仿真就绪"的资产标准：OpenUSD 几何 + 物理属性 + 语义标签 +
材料 + 密集字幕 + 传感器属性。它是优秀的内容规范 —— 但它是**死的**：

> **SimReady 没有智商。** 资产规格由委员会发布后冻结；场景不会因为
> 跑过一次故障而变得更聪明。

我们的核心命题不同：

> **规格应由运行经验持续改写 —— 且每次进化必须过消融门。
> 进化不靠信仰，靠消融。**

## ESR-Loop：自进化闭环（五拍）

```text
① 运行 → ② 经验结晶 → ③ 知识沉淀 → ④ 场景进化 → ⑤ 消融验证
     ↑___________________________________________________|
```

每一拍都有代码与真实样本支撑：

| 拍 | 实现 | 实证样本 |
|---|---|---|
| ① 运行 | 证据事务内核（物理执行 → 证据事件流） | 30min 压测 × 3 + 1200 runs 消融母库 |
| ② 经验结晶 | `esr/crystallize.py`：incident → 四件套（根因假设/修复动作/回归用例/契约修订） | DetFix 真实事故（16 runs → 三修复） |
| ③ 知识沉淀 | Voyager 式技能条目（前置→执行→证据→后置） | `reports/esr/*/knowledge/skills/` |
| ④ 场景进化 | `esr/gate.py`：新类型入册必须附（契约草案+消融子集+回归用例），缺一拒绝 | `CONV-FEED/CONV-OUT` 经真注册表落册 |
| ⑤ 消融验证 | 回归工件回放 / 可注入 30-seed harness | 三回归用例全过（max_pen ≤0.016mm / determinism） |

## 与 SimReady 的能力对照

| 维度 | SimReady | ESR (dc-twin) |
|---|---|---|
| 资产物理/语义/材质 | ✅ OpenUSD 规范 | ✅ 对应层（MJCF+manifest+material_class，OpenUSD 互导为适配器） |
| 证据契约 | ❌ | ✅ 资产声明"什么证据证明我的状态跃迁" |
| 技能接口 | ❌ | ✅ agent 可调技能面（MCP 形状，LLMFacade 协议预留） |
| 规格演化 | 委员会发布后冻结 | **运行经验持续改写，进化门+消融强制** |
| 执行可信 | 不涉及 | 证据门控原子事务（Unsafe 提交 0%，见 agv-world-model 1200 runs 消融） |
| 可复现 | 依赖专有运行时 | 纯开源全链（MuJoCo / three.js / Python） |

## 分层架构

```text
L1    core (agv-world-model): MuJoCo 物理 / CP-SAT 调度 / 锁矩阵
L1.5  证据事务内核: EvidenceVerdict 五门 → 原子 commit → 恢复对账
L2    两级感知 (观测隔离: 决策侧零真值引用)
L3    DC 场景: 产线 (线边库→传送带→冲压→产出) + 30-seed 场景库
L4    agents + ESR-Loop (esr/ 包) + 高保真数字孪生面板 (three.js MJCF 镜像)
```

组合方式：`dc_twin` 依赖 `agv-warehouse-core`（本仓库不复制内核代码），
面板以文件契约读取状态（零 import 耦合），ESR-Loop 经 core 既有注册表
接口落册（不绕过单调语义）。

## 当前状态（v0.1）

- **ESR-Loop v0.1 可运行**：DetFix 真实样本走通五拍
  （`scripts/esr_detfix_walkthrough.py`，产物 `reports/esr/detfix_loop_v0.1/`）
- **产线参考实现**：P0'.5 携托联动（mocap 从动）后全套件绿，demo 零穿模
- **首次自我进化**：CONVEYOR 节点类型经进化门入册
  （`scripts/esr_conveyor_walkthrough.py`）—— 框架第一次"越用越聪明"
- 测试：结晶/进化门/Loop 编排 38 用例 + 产线物理/负向注入用例族

## 在 TopPrism 中的位置

```text
prism-ontology        语义契约 (是什么, 不能混淆什么)
        │
agv-world-model       物理内核 + 证据事务 + 消融母库 (Paper I)
        │
dc-twin (本仓库)      ESR 参考实现: 场景如何随运行经验进化
        │
agent-nurture-framework   组织级经验治理协议 (什么经验可信、谁能晋升)
```

## 相关仓库

- [agv-world-model](https://github.com/topprismdata/agv-world-model) — 物理证据门控的 Agentic 仓储世界模型（内核）
- [prism-ontology](https://github.com/topprismdata/prism-ontology) — 共享语义契约与 Operational Profile
- [agent-nurture-framework](https://github.com/topprismdata/agent-nurture-framework) — 经验沉淀的治理与评测协议
