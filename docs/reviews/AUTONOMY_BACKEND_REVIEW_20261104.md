# FindYou 自主社交后端独立审查

日期：2026-11-04。范围：本轮新增 social / identity / agent / context / capture 与 API、Worker、JobQueue、Gateway、迁移的集成。审查期间只读 src/tests，只新增本报告；没有安装软件、读取真实消息、调用真实平台或真实云模型。

## 结论

发现 1 项 P1、4 项 P2。建议修复后再将本轮实现描述为可持续运行且支持长期人物记忆的 Agent。现有平台发布禁用边界成立：适配器操作白名单无写操作，发布接口返回 platform_write_disabled；本报告未发现能够执行真实发布的新增路径。

证据强度：以下 5 项均为高，具有当前源码路径和隔离临时数据库复现；真实平台/模型表现不在本次验证范围。

## F1 · P1：异常/崩溃后 Run 永久 running，去重和重试共同阻断恢复

位置：`src/findyou/agent.py:90`、`:102`、`:149`、`:321`；`src/findyou/worker.py:100`。

`process()` 先提交 running Run 和 dedup_key，只在 AppError 分支标记 failed。Worker 能捕获普通异常并把 Job 标记 failed，但对应 Run 不会更新。随后相同目标返回已有 running Run，`retry()` 又只接受 failed Run，因此正常 API 无法恢复同一工作。进程在模型请求期间退出会留下同样的持久状态；Job 最终过期也没有同步 Run 的路径。

隔离复现：

1. 使用 `tests/test_agent.py` 的 BiliFixture 捕获一个目标；把 `rt.gateway.generate` 换为抛 RuntimeError 的函数。
2. 入队 agent_reply，执行 `rt.worker.run_once()`。
3. 观察 Job.status=failed，但 AgentRun.status=running。
4. 恢复 fake gateway，调用 `rt.agent.process(target_id)`，得到 `duplicate=True, status=running, output={}`。
5. 调用 `rt.agent.retry(run_id)`，得到 retry_unavailable。

影响：单次未知异常或进程中断会让这一目标/角色/上下文组合无法继续；任务完成状态与运行审计互相矛盾。日常重启后仍然存在，不能通过重新提交修复。

建议：持久关联 Run 与 Job/执行租约，在异常和失联恢复时将 Run 置为可解释的失败或不确定状态；重试采用原子状态迁移，保持旧用量记录，并允许对已失去执行者的 running Run 进行受控恢复。增加普通异常与模型调用期间进程中断的恢复验证。

## F2 · P2：人物记忆以整条当前评论做子串匹配，摘要型和跨评论记忆通常不会进入上下文

位置：`src/findyou/identity.py:228`；`src/findyou/context.py:41`、`:107`。

ContextBuilder 把目标评论前 500 字原样传入 retrieve；retrieve 要求该整串文本出现在记忆正文中。模型提取出的摘要、归纳后的自述和该人物在其他话题的历史资料，即使目前有效且相关，也很难包含完整当前评论。现有抽取测试使用 fake gateway 直接复制原文，所以绕过了这一缺陷。

隔离复现：对 BiliFixture 的根评论作者，使用该评论 ID 作为证据，调用 propose_memories 保存一条有效 self_report 摘要 `This contact prefers being understood before receiving advice.`。`identity.retrieve(contact_id)` 返回 1 条，而 `agent.context.build(target_id)['contact_memories']` 返回 0 条。证据仍有效，失去召回仅由匹配条件造成。中文改写摘要、不同话题的后续评论也具有同样的匹配障碍。

影响：记忆库可以写入、列表可以显示，但生成时几乎只会召回原文重复的记忆，无法支撑用户要求的跨评论长期人物记忆。

建议：在当前联系人隔离与权限过滤之后，采用适当的关键词/相关性检索并提供有限近期记忆回退；无需为此直接引入向量库。最终持久化前仍按所选记忆 ID/版本重新校验。增加“旧自述摘要 → 新表达/新话题”的上下文断言。

## F3 · P2：contact_extract 的 Job.result 保留撤销/删除前记忆原文，绕过当前读取脱敏

位置：`src/findyou/agent.py:182`；`src/findyou/worker.py:41`、`:105`；`src/findyou/jobs.py:22`、`:63`；`src/findyou/api.py:195`、`:199`。

contact_extract 返回完整 memories；Worker 把它原样保存到 Job.result；任务列表和任务详情直接返回持久化 JSON，不执行人物记忆的当前来源/删除校验。当前联系人记忆接口已经会把失效版本 text 脱敏为 None，因此这不是一致的历史查看策略。

隔离复现：入队 contact_extract 并运行成功；把评论来源 Content.visibility 改为 private。此时 `identity.list_contact_memories(contact_id)` 的 text=None，但 `queue.get(job_id)['result']['memories'][0]['text']` 仍是完整原文。GET `/api/v1/jobs/{id}` 和 GET `/api/v1/jobs` 使用该相同未过滤路径。删除记忆也不会修改旧 Job.result。

影响：撤销来源或删除记忆后的读取语义存在旁路。该接口仍需要管理 token；本次没有声称存在无认证泄露或撤销后继续送往模型的路径。

建议：任务结果只保存 memory_ids/run_id/simulated 等引用；向管理界面展示时通过当前权限和删除状态重新读取。为历史已保存结果补动态脱敏，并覆盖任务列表与详情两条路径。

## F4 · P2：停用 Mission 后，已入队的该 Mission 仍会读取平台并消耗模型预算

位置：`src/findyou/agent.py:255`、`:264`；`src/findyou/worker.py:37`。

set_mission(False) 只修改 Mission.enabled。schedule_due 已入队的 Job.payload 包含 mission_id，但 Worker.handle 调用 cycle 时完全不读取它。用户在任务执行前关闭 Mission 后，该任务依然执行。

隔离复现：创建 enabled Mission → schedule_due() → set_mission(id, False) → worker.run_once()。BiliFixture 记录新增 2 次读取（video_info/comments），AgentDraft 新增 1 条。

影响：Mission 开关无法停止已排队的自主活动和模型花费；全局暂停仍可生效，但它会停止其他任务，不能替代单 Mission 停用。

建议：执行 Mission Job 前校验其当前存在、归属和 enabled 状态；对停用的已排队任务作可解释的取消/跳过。若还需要停用进行中 Mission，则在周期边界通过 heartbeat 同步校验。覆盖“入队后、执行前关闭”的用例。

## F5 · P2：max_targets 仅限制成功决策数，失败时可多次调用模型超过本次目标上限

位置：`src/findyou/agent.py:213`、`:226`、`:229`。

循环用 `len(output['decisions'])` 判断 max_targets，只有成功 process 才加入 decisions。模型输出无效等失败会继续尝试下一条，因此 max_targets=1 并不限制最多处理 1 个目标。每日预算最终能阻止无限消耗，但不能保证本周期的设置。

隔离复现：fixture 返回 3 个有效根评论；设 max_targets=1；gateway 每次抛 AppError(model_output_invalid)。执行 cycle 后生成 3 条错误和 3 条 AgentUsageReservation。

影响：上游模型持续格式错误时，原本只要求 1 个目标的周期可能用完每日调用额度，也对更多联系人进行了未预期处理。

建议：在尝试目标前增加独立 attempts/targets_considered 计数并执行上限，分别展示成功、跳过和失败；若需要补足成功结果，应另设清晰且有界的重试预算。增加错误模型与部分失败场景的调用次数断言。

## 已验证与尚未验证

- 命令：`.venv/Scripts/python.exe -m pytest tests/test_social.py tests/test_identity.py tests/test_agent.py -q`。
- 结果：47 passed，29.25 秒；1 条 Starlette/httpx 弃用警告。测试通过不能覆盖上述 5 项已复现缺陷。
- 隔离复现使用 TemporaryDirectory、`_env_file=None`、sqlite DELETE journal、BiliFixture 和 fake/替换 gateway，没有接触真实 data 目录或真实账号。
- 检查了来源可见性过滤、联系人证据归属、角色批准、生成后再校验、调用预算预留和发布禁用的真实服务路径；当前未发现新增平台写旁路。
- 未重新跑旧全量测试、生产库升级/备份恢复、真实连接器返回结构、实际云模型风格/注入抵抗、真实计费、长期运行并发/硬终止压力测试。没有将 prompt 中的行为要求视为经过实证的模型安全能力。
- 本报告依据审查时工作树；主代理后续修复应单独标记验证结果，不能把本报告当作修复后通过证明。

## 第二轮独立复核

主代理根据上述结果修改后，重新阅读了真实服务路径，并运行相同三个测试模块：**54 passed，36.07 秒**，仅原 Starlette/httpx 弃用警告。以下状态是本轮修正的独立复核，不覆盖首次审查的历史证据。

| 首轮问题 | 复核状态 | 当前证据 |
| --- | --- | --- |
| F1 异常后 running 卡死 | 普通异常已修复；增加失联恢复，但另见 F6 并发边界 | process/extract/cycle 普通异常安全标 failed；回归覆盖异常后显式 retry 与超时 Run 状态恢复 |
| F2 人物记忆召回 | 已修复原缺陷 | 改为中英文词面重叠和最多两条近期回退；人物隔离、状态和来源过滤仍在；不将词面得分宣传为语义相似度 |
| F3 contact_extract 任务缓存原文 | 新任务已修复 | Worker 只保存 run_id / memory_ids / simulated，回归通过；未对既有旧任务历史做迁移验证 |
| F4 停用后已排队任务仍运行 | 已修复原复现 | Worker 领取及 heartbeat 校验 mission.enabled；关闭后无读取、无 Run 的回归通过 |
| F5 失败调用绕过 max_targets | 已修复原复现 | 独立 attempts 计数限制失败尝试；回归断言 max_targets=1 仅 1 次预留 |

### 撤权接口一致性的追加核查

第二轮曾用合法的合成模型返回值，在 answer/reason 中复述合成源评论：撤销来源后，单条 social_decision Run 已脱敏，但 agent_reply Job 的 reason、cycle Run 的子结果、CLI agent-runs 的原始 snapshot，以及 PATCH 草稿返回的历史 versions 仍能携带旧内容。该问题已反馈并在本轮由主代理修正。

对修正后代码再次执行隔离 API/服务复现，确认：GET jobs 不再含 reason 原文，agent.runs 返回经过 get_run 校验的视图，PATCH 已失效草稿返回 409；cycle decisions 与 agent_reply Job 均只保留 Run 引用信息。没有把持久化 JSON 当成已经通过当前授权校验的可展示正文。

### F6 · P2：超时恢复缺少对迟到旧执行的提交隔离（本轮发现，已修复原复现）

位置：`src/findyou/agent.py` 的 `recover_abandoned()`、`process()` 最终写入事务和 `retry()`。

recover_abandoned 依据 created_at 把旧 running Run 改为 failed；仍在执行的旧调用在返回后，没有检查 Run 是否已失去有效执行状态，就能把状态改回 succeeded 并保存草稿。如果此时已显式 retry，新旧两次执行都能各产生草稿，破坏恢复后的幂等语义。

隔离最小复现（高强度状态机证据）：让第一个 fake gateway 在线程中阻塞；把对应 Run.created_at 调整为超过恢复阈值；调用 recover_abandoned，确认状态 failed；恢复正常 gateway，retry 并执行新任务；释放旧调用。结果 AgentDraft 数量为 2，旧 Run 被迟到结果复活。这里用可控时钟状态加速超时，没有等待真实网络或声称真实供应商已经发生此情形。

触发条件限制：需要旧执行在恢复时实际上仍存活。源码把 model timeout 视作总时限，但 httpx 网络超时并非完整请求的绝对墙钟截止时间，慢速分块响应或进程暂停后恢复可能越过阈值。当前没有真实供应商复现，也没有平台发布风险；这是明确的并发提交边界问题，因此标 P2。

建议：最终单写者事务内校验 Run 仍由本执行持有且处于有效状态，或使用执行 token/租约进行条件更新；已被恢复器判失联的执行不得再次提交正文。保留消耗过的预算，增加“恢复 → 重试 → 旧调用迟到返回”的并发回归。当前 F1 的正常异常恢复已改善，但不能据此宣布所有中断/并发恢复场景通过。

### 最终定点复核结果

主代理在 process 最终 BEGIN IMMEDIATE 写入事务内检查对应 Run.status 仍为 running；已恢复的旧执行抛 run_execution_lost。重新执行原阻塞 gateway 并发复现，结果：旧调用返回 run_execution_lost，AgentDraft 总数为 **1**，旧 Run 维持 **failed**，没有被迟到响应改回 succeeded。F6 的双草稿复现已关闭，证据强度高。

撤权追加修正后的三个模块测试再次得到 **54 passed，37.35 秒**；随后 F6 的最后修改单独用上述并发场景复核。该最后一步没有再重复整套测试，不能将 54 项结果表述为最终提交状态的全量验证。

当前结论：本报告首轮 F1–F5 和追加的 F6 所列具体复现均已修复；在已经检查和执行的范围内，没有尚未关闭的 P1/P2 发现。仍未执行真实平台/云模型、硬终止多进程压力测试，以及全部类型 Run 的租约生命周期组合；特别是本次 F6 定点验证仅覆盖 social_decision 迟到结果，不能据此推广到 contact_extract/cycle 的完整中断恢复。原始报告与第二轮发现段落保留为历史证据。
