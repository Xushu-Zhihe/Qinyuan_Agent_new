# D2 可恢复复盘闭环：实施前契约记录

日期：2026-10-02。状态：**契约已冻结；不是 D2 验收记录**。

已确认范围：沿用 D1 固定摘要验证，只证明附件副本于保存观察时符合绑定摘要，不代表整题成功；继续建议只接续未完成流程，不重新打开 Intent 或重复工具。约束只能收紧已有许可，绑定当前 Intent/Turn，不得修改目标、完成标准、授权、总预算或 Deadline。先验收内部模拟及关键中断恢复。

已确认额度与恢复：每个复盘根归属最多 2 次实际外层调用，TaskRun 内 D2 复盘调用共用硬上限 8；不设置触发原因数量上限。上下文刷新、结构纠正、语义拒绝、空响应和无效响应共用同一次再生成机会，generation 不重置 attempt。已发调用即使 provider failure、认证失败或未知也消耗额度；这些结果没有可复用复盘，恢复保持暂停，不创建新归属、不重发。查询、取消、清理始终允许。只有已知可重用结果或剩余额度内确定性拒绝/空响应/无效响应可按显式恢复规则继续。

已确认身份：`action_candidate_id` 是首次内层动作请求的可信内部台账 ID；每次内层调用有独立 `action_model_request_id`，结构纠正仍归属原候选根。供应商 `provider_request_id` 只作可选审计元数据，不能作主键或未知结果重发依据。复盘使用 `review_root_id`、`review_generation`、`review_attempt`、`context_version`；已应用决策不可变，新 generation 共享原额度。

已确认约束执行边界：allowed_action_kinds 取现有许可与所有有效 Intent/Turn 限制交集，超时取更小值；禁止放宽，违规直接拒绝，不静默裁剪模型候选。`max_action_timeout_seconds` 只影响工具动作，不改模型调用超时、可信验证规则或 TaskRun Deadline。约束在所属 Intent/Turn 结束时失效但保留历史，恢复时继续校验仍有效的约束。`probe` 派发后按原执行边界结束并保存结果，新约束用于后续动作；已生成未派发候选须按最新约束复核；`complete_intent` 在完成提交前按最新约束复核。空交集/动作额度耗尽时暂停并记具体原因；取消、Deadline、未知按原路径处理。暂停允许查询、取消和清理。

已确认尾复盘顺序：`独立验证 -> 原子提交 Turn.completed -> 只读尾复盘 -> 保存结果`。尾复盘失败保留待处理记录、停止后续派发，不回退 Turn；不新增约束、不重开 Intent、不改完成标准。取消/Deadline 阻止尾调用时记录未调用原因，仍允许清理。

已确认轮内时序与信号：复盘在固定只读 Action 的结果/证据保存后、Intent 完成判定前。触发包括内层 request_review、可信代码确定的 not_satisfied 和执行前/后的确定性动作拒绝；执行前拒绝只要求保存原提案、拒绝及审计，不伪造 Execution。模型猜测、证据缺失或无法判断不能归为 not_satisfied；未知直接恢复。运行中的请求只登记，结果/证据保存后才处理。普通成功且无请求不复盘。

已确认去重与包装：Action 复盘归属键包含 TaskRun、Turn、Intent 和 `action_candidate_id`；首尾复盘分别使用 `(TaskRun, Turn, review_scope)`，且 `action_candidate_id=null`。同一上下文版本合并全部触发原因；可信结论或来源版本变化时，未应用旧建议失效，刷新后在原 root 的剩余额度内新建 generation。未知不能用新代次重发，已应用记录不改写。D2 专有包装的 action 保留完整原类型和参数，`review_requested` 为必填 bool；两者一同登记。probe 保存结果/证据或确定性拒绝后消费请求，未知保留请求并先恢复。complete_intent 顺序为候选/请求登记、准备验证依据、按需复盘、可信校验建议和状态、独立完成判定、原子提交；继续建议不是完成许可。

本文依据 Architecture.md、interface.md、protocol.md、restrict.md、TODO.md 和 Qinyuan_Agent_dual_llm_flow.svg，以及当前未提交的 D1 实现编写。保留现有工作区改动，未修改业务代码或把 D2 勾为完成。

## 当前证据与交付边界

- TODO.md:286–293 要求 Turn 首尾复盘、触发式 Action 后复盘、有来源建议、Intent/Turn 作用域，以及刷新、受限重生成、暂停和未知不重发；具体信号和次数须在本轮启动前确认。
- Architecture.md:17–19、interface.md:18–20 和 protocol.md:20 明确复盘是建议，不能代替独立验证、扩大授权或直接写控制状态。
- SVG 的 routing 节点要求在 Action 结果和证据保存后复盘，再按可信状态路由；兄弟切换与新 Plan 属 D3。
- agent_core/turn_modeling.py:138 的请求目前只有 outer/probe/complete 三个 stage。agent_core/turn_contracts.py:19–31 的外层预算为两次规划调用，不包含复盘。
- agent_core/single_turn_runner.py:692–713 保存成功 probe 的 Plan、首次摘要、证明和 Fact，随后进入 complete_intent；这是成功 Action 后复盘的最小接入点。
- agent_core/single_turn_runner.py:737 直接提交 Turn 完成。agent_core/sqlite_control_store.py:1367–1368 在 Turn.completed 后拒绝所有新增 D1 操作，因此尾复盘时序不能从旧实现直接推定。
- D1 暂停只有应用停止结果，TaskRun 没有 paused 枚举；已保存操作、模型原 envelope 和拒绝收据可复用恢复设计。

建议的最小路径：

`新 D2 TaskRun -> D1 同款外层目标/单候选注册 -> 首复盘 -> 可信接受约束 -> 内层固定检查 -> 保存 Execution/摘要/Fact -> 按信号复盘 -> 内层完成 -> 独立 Turn 规则判断与完成提交 -> 只读尾复盘 -> 分层结果`

上面采用已确认的完成后只读尾复盘时序。

仍使用一个本地附件、一个候选、一个获选 Intent、固定只读 SHA-256 工具、Fake 模型/Fake Sandbox 与真实本地持久化。本轮不实现 D3 的候选切换/新 Plan/下一 Turn、D5 的 Process_Fact 或 D7 的公共 CLI/工作台接入。

## 决策 1：范围与首尾时序

**已确认范围：**沿用上述最小范围。首复盘建议在 Turn/规则注册并选定当前 Intent 后、首个内层动作前。

**已确认：**先由可信代码独立验证并原子提交 Turn.completed，再执行只读尾复盘。尾复盘失败记录待处理并停止后续派发，不回退已完成 Turn。尾复盘使用 `acknowledge_turn_end`，只检查并总结已提交过程来源；constraints 必须为空，不接受新约束，不能重开 Intent 或修改完成标准。D2 存储需允许 completed Turn 的只读尾调用和结果/失败记录。

取消或 Deadline 阻止尾调用时，不派发模型调用，持久记录 `not_called` 及对应原因并允许 Sandbox 清理；需确保该审计记录不回滚既有终态且不能开启其他执行操作。未知结果或证据不可复核直接停在恢复边界。

## 决策 2：轮内信号与内层请求表达

**建议触发信号：**

1. 内层在动作候选中显式请求复盘。
2. 执行前或执行后的确定性动作拒绝：原提案、拒绝收据和审计保存后，可信侧触发复盘；执行前不要求有 Execution。
3. 工具和首次摘要观察均已完成、证据已保存，且可信代码确定固定执行预期 not_satisfied 时触发；模型猜测、证据缺失和无法判断不属于该信号。

普通成功 Action 没有请求则不复盘。无法确认模型、工具、摘要观察结果或证据完整性时直接 recovery_required，不继续调用外层。

已确认内层响应使用 D2 专有包装：`{action: <已有 probe 或 complete_intent>, review_requested: <bool>}`，review_requested 必填、严格 bool，action 保留完整原类型和参数。probe 的请求先登记，工具/观察及证据保存后消费；complete_intent 的请求在候选与审计保存、准备验证依据后，Controller 完成判定/提交前消费，不能等 Intent.succeeded 后再请求继续。复盘后仍须独立判定和原子提交；D1 响应格式保持原样。

已确认触发去重：归属键包含 TaskRun、Turn、Intent 与当前动作候选处理 ID；当前单候选请求使用模型请求 ID，不补造 Execution。同一动作、同一上下文版本的请求和可信信号合并为一次复盘，保存全部原因；调用前冻结上下文。后补原因若只是同快照说明则追加审计；含新可信结论或来源版本变化则复核适用性，未应用旧建议失效，刷新后预算内新建代次。未知不得借新版本重发，已应用记录保留历史。Turn 首/尾各有独立归属键；恢复复用原记录。

独立 request_review 动作本轮不采用；请求字段不增加一次内层调用，实际外层复盘仍单独计账。

已有工具/首次摘要额度不增加。摘要确认不满足后，即使外层建议继续，也不能再次检查或绕过目标标准；当前 D2 没有可行后续动作时，记录 `no_safe_continuation_in_d2` 并暂停，后续路径留待 D3。确定性动作拒绝后的新候选至多占用该内层阶段剩余的一次调用，结构纠正和语义重生成共享原阶段上限，不另加次数。

## 决策 3：复盘上下文、输出与约束

**建议输入：**可信 envelope 固定 TaskRun/Turn/Intent、复盘 ID、触发类型及源 Action、Checkpoint、上下文摘要、角色配置引用、当前约束版本。上下文读取已注册目标/标准、当前执行预期、已提交 Execution/Fact/拒绝、可校验来源引用及证据摘要；首复盘无 Execution 时使用目标绑定和导入收据来源。普通上下文不放原始模型响应、脚本、stdout/stderr 或凭据。

**建议模型输出字段：**

- `decision`：首/轮内为 `continue_current_intent`，尾为 `acknowledge_turn_end`。
- `rationale`：有界理由，只作模型判断，不能成为 verified Fact。
- `source_ids`：当前上下文提供的来源标识；禁止跨运行/归属不符/不存在的引用。
- `constraints`：零项或多项经注册的约束；尾复盘必须为空。

来源标识由可信代码在输入中给出；模型不能自行指定 ID 或版本。可信侧复核来源、证据可用性、Checkpoint、作用域和约束版本，接受后才允许下一步。叙述不是验证结论，不依据文字中的“完成”提交成功。

上下文版本由目标/规则绑定、可信结果、来源证据版本/内容摘要及有效约束版本确定；Checkpoint 单独保存在请求中。单纯追加触发说明、模型开始/完成账目和暂停审计不单独改变语义版本；实际控制状态/来源/可信结论/约束变化触发适用性复核。新 generation 共享 root 的剩余 attempt 和 TaskRun review 额度，不重置次数。已应用决策与约束保留不可变历史；同归属上下文实质变化时，可在原 root 剩余额度内增加 generation。新的动作候选、下一 Turn 或新 TaskRun 才创建新的 review root。

**已确认的首期两种约束字段：**

| kind | 值 | 执行方式 |
| --- | --- | --- |
| allowed_action_kinds | probe / complete_intent 的非空子集 | 下一个动作可信准入前检查；多项有效约束取交集 |
| max_action_timeout_seconds | 1–300 的整数 | 仅当前 Intent/Turn 内新工具动作的局部超时上界；多项有效约束取最小值，仍受已有 Deadline/Policy 上界限制 |

用户已确认只收紧、作用域与历史边界。每项约束携带 `scope_kind`（intent 或 turn）及当前对应 `scope_id`；不得引用其他对象。约束追加版本、不能撤销已接受限制；Intent 终态/Turn 终态后对应约束失效并保留历史。约束仅裁定未来派发，不改变执行中 probe。新候选中不符合有效约束的动作保存确定性拒绝，不擅自改写模型参数后执行。没有可行下一动作或动作额度耗尽则暂停并记录原因。

建议具体准入语义：有效允许集合取基线许可与所有有效约束的交集；更宽集合或大于已有有效超时上界的新约束候选拒绝，允许相同值作为冗余记录。内层收到当前有效约束与版本；probe.timeout_seconds 超过有效上界即拒绝，不靠裁剪执行违约候选。约束只影响未来尚未派发动作，不追溯修改运行中 probe。执行中 probe 按原 timeout、Policy 和 Deadline 结束后，所有新动作候选按最新约束复核；此前已生成但未派发的候选也重验。`max_action_timeout_seconds` 不作用于模型调用超时、可信验证规则或 TaskRun Deadline。作用域结束失效不恢复已耗尽额度或终态对象的执行资格。

示例（来源 ID 在真实请求中由可信上下文提供）：

```json
{
  "decision": "continue_current_intent",
  "rationale": "已保存的检查结果可进入完成请求阶段。",
  "source_ids": ["<current_execution_source>", "<current_fact_source>"],
  "constraints": [
    {
      "kind": "allowed_action_kinds",
      "scope_kind": "intent",
      "scope_id": "<current_intent_id>",
      "allowed_action_kinds": ["complete_intent"]
    }
  ]
}
```

未知字段、任意脚本、目标/规则/授权/总预算/Deadline 修改、兄弟切换和重规划建议均保存拒绝，不执行。

## 决策 4：刷新、重生成与模型错误

**已确认：**每个独立触发最多两次实际外层调用，初次加一次再生成。结构纠正、确定性语义拒绝、过期提案刷新，以及已知的 empty_response/invalid_response 共用这一次机会，不形成嵌套重试。provider failure、认证失败和未知调用也消耗额度，但没有可复用结果，不自动重发。

每次新调用前重新加载已提交状态、核验证据和当前约束版本。保存原提案、原 envelope、拒绝码/错误、新的请求来源关联与计数；旧提案不应用，不借旧建议继续。与当前已提交状态一致且已保存的未应用结果只读复核后重用，不替换调用。

认证失败、明确拒答及 provider_failure 不自动重试；网络/SDK 层维持零自动重试。只有缺少可靠完成收据的开始记录属于未知；无法确认结果的超时/断线直接恢复，不重新派发。已知收到但被拒绝、过期或未应用的响应不能记为未知。

## 决策 5：限额、暂停与显式恢复

**已确认：不设置独立触发数量上限，只用实际调用额度限制：**

| 类别 | 建议上限 |
| --- | --- |
| 每个独立复盘触发的实际外层调用 | 2（初次 + 1 次共享纠正/再生成） |
| 当前单 Turn 的独立复盘触发 | 不单独设上限；每次触发消耗实际调用额度 |
| 当前 TaskRun 的 D2 复盘实际调用总计 | 8 |

数值仅适用于 D2 内部切片配置，不写成框架通用阈值。外层规划保留 D1 的 2 次；内层总计 4 次、probe/complete 各 2 次；工具/首次摘要各 1 次。D2 review 调用池与 D1 规划/动作台账分开计数；所有实际调用开始前持久计账，取消、拒绝、过期、恢复不退还次数。尾复盘因取消、Deadline 或额度不足未调用时只保存 `not_called` 审计，不消耗实际调用额度。

失败达到本触发上限时应用返回 stopped/paused，TaskRun 仍 running，Sandbox retained，保存暂停原因和恢复所需版本，不增加 TaskRun.paused 枚举。已提交 Fact/Intent/Turn 状态不回退；整体目标仍 not_evaluated。

显式内部恢复先核对固定配置、原触发与请求 ID、Checkpoint、来源、约束版本、开始/完成/应用记录。中断留下的已知结果按相同 ID 重用；剩余重生成额度可继续，已耗尽的触发返回原暂停，不重新开启一轮或补次数。provider failure、认证失败、明确拒答和未知调用保持暂停，resume 不再调用外层；只有新 TaskRun 才能再次尝试，且不继承旧授权、验证结论或复盘结果。查询、取消和清理保留。

## 决策 6：兼容与验收方式

建议新建明确的 D2 链标识和冻结配置，添加窄类型、控制器操作及持久化迁移，复用 D1 的导入、固定工具、观察和独立目标证明。旧 D1/旧 T5/T6 不回填复盘、约束或额度，不因打开状态/重放自动升级运行。只读读取兼容历史 schema，迁移仅在可写打开时执行。

验收使用 Fake 模型/Fake Sandbox、本地 SQLite 和不可覆盖证据，至少覆盖：

1. 首复盘、单次请求触发与尾复盘的完整链路及分层结果。
2. 成功且未触发的 Action 不额外复盘；Action 结果/证据保存之前不能复盘。
3. Intent/Turn 作用域有效、终态失效；约束收紧、冲突和跨对象来源拒绝。
4. 目标/授权/总预算/Deadline 修改拒绝；固定工具权限不扩展。
5. 确定性拒绝后刷新再生成成功；连续失败暂停；旧提案不得继续使用。
6. 未知模型不重发，已保存未应用结果重用；调用与约束应用跨重启无重复。
7. 取消/Deadline/迟到与过期响应不派发后续动作；预算跨恢复不清零。
8. 缺失/损坏证据不冒充成功；尾复盘失败保留分层状态。
9. D1 与所涉共享控制面的回归、定向静态/类型/编译、离线构建与差异检查。

确认后按窄类型声明、纵向实现、失败/恢复分支和实际验收推进，同步四份规范及 TODO。未通过本地验证不宣告 D2 完成；Fake 验收不代表真实模型/Docker/目标网络或 D7 公共入口通过。

## 本次基线复验

四个现有 D1 测试文件于本次离线复验取得 **74 passed、1 warning，35.84 秒**；记录为 `.test/d2/d1-baseline-verified.xml`。这只是当前 D1 依赖基线，不是 D2 新功能验收。

受限执行先在 `.test/d2-baseline-tmp` 出现 pytest `PermissionError [WinError 5]`，该运行不计为通过。随后经自动审批允许的沙箱外本地模拟执行取得上述结果；未连接真实模型、Docker daemon 或题目目标。warning 是禁用 cacheprovider 后的现有 `cache_dir` 配置提示。

```powershell
. ./scripts/test-env.ps1
uv run --offline --no-project --with pytest --with docker --with 'mcp==1.29.1' python -m pytest tests/test_d1_chain.py tests/test_d1_workspace.py tests/test_d1_persistence.py tests/test_d1_modeling.py -q -p no:cacheprovider --basetemp=.test/d2-baseline-verified-tmp --junitxml=.test/d2/d1-baseline-verified.xml
```
