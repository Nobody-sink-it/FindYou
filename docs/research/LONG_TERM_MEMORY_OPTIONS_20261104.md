# FindYou 长期记忆方案调研与接入建议

调研日期：2026-11-04。范围：官方 GitHub、官方文档及当前 FindYou 源码；只读调研，未安装候选包、未运行候选服务、未发送项目资料或真实交往数据给模型，未修改 `src`。本文的能力核实是静态证据，不代表 Windows、DeepSeek 或真实数据端到端验收。

## 1. 决策建议

**当前保留 Python + SQLite 为事实、来源、权限、版本的主库，按现有 S2/S3 设计扩展人物记忆与语境构建。先建立一个小的可替换检索接口；暂不把 Mem0、Letta 或 Graphiti 作为主记忆系统。** 这是针对当前单用户项目的工程判断，证据强度中；不是 SQLite 在所有规模或所有问答任务中更优的结论。

- **第一备选为 Mem0 OSS 的隔离检索副本**：当固定中文用例证明现有词法检索漏召回时，再做合成数据试接。只索引应用已判定可用的记忆，使用直接导入模式，结果按主库当前权限/版本再核验。若只需要向量检索，直接使用成熟向量存储也可能比再加自动记忆框架更简单。
- **Graphiti 留给已证实的跨人物、跨时间关系查询需求**：它的时间关系和 episode 来源链更接近长期交往，但会增加图库、实体消歧、多次模型抽取及派生删除的复杂度。
- **Letta 暂不接入**：当前活跃产品已迁移到 TypeScript Agent harness、App Server 与 MemFS。引入它会改变运行时与记忆管理权，超出复用一个 Python 记忆组件的范围。
- **SimpleMem 只借鉴压缩和多路召回思路**：文本核心仍缺本项目所需的主体、证据、确认、单条撤销契约；不能因为名称包含 Simple 就判定迁移更轻。

最关键的能力缺口是“何种证据可以改变谁的什么记忆”，不是是否能存储一段摘要。以上候选均不能替代 FindYou 自身的认识类型、权限和确认规则。

## 2. 已核实的项目基线

以下为本轮源码事实，证据强度高；仅描述列出的模块，不把设计稿当作已实施功能。

| 当前证据 | 已有能力 | 实际缺口 |
|---|---|---|
| `src/findyou/content_models.py` | Content 区分 owner/external、private/public；快照/块保留来源；Memory/Version/Evidence 分表，有 candidate/confirmed/inactive | Memory 目前只有 owner_id，没有独立 contact/subject、认识类型、有效时间和用途权限 |
| `src/findyou/storage.py` 的 `propose_facts`、`confirm_memory` | 外部 Content 不得生成主人事实；证据必须来自该主人资料；确认时再查来源 | 应扩展人物自述/观察/推断，不能直接移除原主人来源门槛以兼容所有人物 |
| 同文件 `correct_memory`、`retrieve`、删除方法 | 纠正新建版本并退回候选；检索在 SQL 中先过滤确认、owner、公开范围及证据；删除内容会停用相关记忆并清索引 | 目前词法召回按更新时间排序，没有语义召回、时效排序；纠正继承旧证据，应另保留本次主人纠正事件作为直接依据 |
| `src/findyou/ops.py` | Conversation 按 owner/account/platform/peer/thread/scope 区分；Message/Action 有 evidence_ids，已有 Job、LLMCall、AuditEvent | 会话摘要不等于跨线程人物知识；当前类型不能单独表示偏好学习或稳定人格 |
| `docs/design/round-03/DATA_CONSOLE_SPEC.md`、`DEVELOPMENT_PLAN.md` | 已规定 Contact + PlatformIdentity、每人物池、版本/有效期/来源、角色版本、运行归因；明确复用现有主库/Job/Gateway | 这是追加实现契约，不是已通过的测试结果 |
| `docs/design/round-04/AUTONOMY_AND_CONTEXT.md` | 明确主人事实、对象记忆、表达反馈、关系估计分开；外部评论/AI输出不能变主人事实；人格稳定与角色卡分层 | 学习成效必须用后续实际输出验证，不能由“已存摘要”推出 |

因此迁移成本应按“保留上述约束后的适配成本”比较，而非比较各项目的 quickstart 行数。

## 3. 上游维护和许可证快照

本轮实际读取官方仓库及 GitHub 公共 API。下表为调研时默认分支最后一次提交的 UTC 时间和 SHA 前缀，证明有可追踪维护活动；不等于发布质量、响应 SLA 或没有漏洞。未使用星数作为决策证据。主分支可在调研过程中变化，后续试接应固定完整 SHA 或明确包版本。

| 项目 | 核实许可证 | 最后提交 UTC / SHA | 维护解释 |
|---|---|---|---|
| [Mem0](https://github.com/mem0ai/mem0) | Apache-2.0 | 2026-11-04 10:55:26 / `ed1f10fc8708` | 主线持续变化；已有 v3 迁移差异，旧教程参数不可直接照搬 |
| [Letta 旧仓库](https://github.com/letta-ai/letta) | Apache-2.0 | 2026-10-18 22:28:01 / `8816d3a85168` | README 明确 V1 服务端退役到 archive，并指向新仓库 |
| [Letta Code](https://github.com/letta-ai/letta-code) | Apache-2.0 | 2026-11-04 07:41:15 / `e6d6cc27ef11` | 当前活跃源码；不能以旧仓库停止更新断言 Letta 整体停止维护 |
| [Graphiti](https://github.com/getzep/graphiti) | Apache-2.0 | 2026-11-03 20:41:02 / `fb60e59d4a20` | 有当前源码与发布；本轮 release API 为 v0.31.2，2026-10-20 |
| [SimpleMem](https://github.com/aiming-lab/SimpleMem) | MIT | 2026-07-24 07:40:38 / `db80b6a7c591` | 本轮 release API 为 v0.3.0，2026-05-21；较最近三者更新间隔更长，不据此断言废弃 |

可复核入口：各仓库 `https://api.github.com/repos/{owner}/{repo}/commits?per_page=1` 与 `/releases/latest`。Mem0 `/releases/latest` 当前返回 TypeScript `ts-v3.3.1`，**不能把它当作 Python SDK 版本**。许可证只说明这些开源代码的许可；云服务、模型、嵌入模型及数据库各自条款另计。

## 4. 候选评估

### 4.1 Mem0 OSS：最适合做后续可丢弃的检索副本

**已核实，证据高。** OSS `Memory` 与托管 `MemoryClient` 是不同产品路径。OSS 可本地运行，配置 LLM、embedder、向量后端，历史另放 SQLite；默认向量后端为 Qdrant，可指定本地路径，不必购买 Mem0 Cloud。它不是把向量直接放进 FindYou 现有业务表。参考 [配置](https://github.com/mem0ai/mem0/blob/ed1f10fc87080f0b67f99be370817504888462ab/mem0/configs/base.py)、[Qdrant 配置](https://github.com/mem0ai/mem0/blob/ed1f10fc87080f0b67f99be370817504888462ab/mem0/configs/vector_stores/qdrant.py)。

**DeepSeek 可配置但未实测。** 官方源码存在独立 DeepSeek adapter，经 OpenAI client 调用可设置模型和 base URL；这比仅声称“OpenAI-compatible 应该能用”更强。仍需单独配置嵌入提供方，不能把聊天 API 兼容等同于拥有 embedding 接口。参考 [DeepSeek adapter](https://github.com/mem0ai/mem0/blob/ed1f10fc87080f0b67f99be370817504888462ab/mem0/llms/deepseek.py)。

**作用域/纠正/过期/删除的边界，证据高。** `user_id/agent_id/run_id` 与 metadata 可用于索引分区；`infer=False` 可直接存储已整理内容；有 update/delete/history，`expiration_date` 按 UTC 日期隐藏过期结果。日期过期不是秒级有效区间，也不等于物理删除。`delete` 会删向量，但 `_delete_memory` 仍把旧文本写入 DELETE history；其 SQLite messages 表也有自己的保留逻辑。因此“删除 API 成功”不等于所有副本已清除。参考 [主实现](https://github.com/mem0ai/mem0/blob/ed1f10fc87080f0b67f99be370817504888462ab/mem0/memory/main.py)、[历史存储](https://github.com/mem0ai/mem0/blob/ed1f10fc87080f0b67f99be370817504888462ab/mem0/memory/storage.py)。

**不能假设的能力。** 用户分区字段不是应用授权；metadata 中附证据 ID 不会自动保证证据属于该人物、仍然有效、允许送云模型。当前 v3 文档还区分 OSS 与 Cloud 的反馈、导出、时间功能，不能把托管功能算进自部署能力。参考 [官方 Python 对比/迁移说明](https://github.com/mem0ai/mem0/blob/main/skills/mem0/client/python.md)。

**应用判断，证据中。** 适合作为“已批准记忆 → 语义召回”的可重建副本。最小模式使用组合 scope（owner + subject + domain），metadata 保留业务 memory_id/version；任何结果都回查 SQLite，不让 Mem0 自行合并、改写主人事实。如果只用 `infer=False` 和向量召回，Mem0 的自动抽取收益会减少，应和直接向量库比较维护成本。

本地路径要明确设在项目数据目录；不能保留示例 `/tmp` 路径。未来试接须在 import 前关闭 `MEM0_TELEMETRY`，源码默认启用；本轮没有初始化库。[遥测源码](https://github.com/mem0ai/mem0/blob/ed1f10fc87080f0b67f99be370817504888462ab/mem0/memory/telemetry.py)。

### 4.2 Letta：人格与持续状态思路有价值，当前接入范围过大

**必须更新的事实，证据高。** [旧仓库 README](https://github.com/letta-ai/letta/blob/8816d3a8516893bfdc0fa14bfb1235226012e295/README.md) 明确现役源码是 `letta-code`，V1 API server 已退役。旧 Docker 服务使用 PostgreSQL，旧 memory blocks 支持 `read_only`，但这些页面现已归到 `v1-sdk`，不能当作当前 SDK 同名能力保证。参考 [旧 Docker 文档](https://docs.letta.com/v1-sdk/docker)、[旧 memory blocks](https://docs.letta.com/v1-sdk/memory/memory-blocks)。

**当前能力，证据高。** 当前 Agent SDK 通过 persona/human 起始记忆和 MemFS 管理状态：记忆是 git 仓库内 Markdown，`system/` 每轮进入系统上下文，其他材料按需读取；dreaming 可后台整理经验；共享记忆在托管部署或自备 git remote 下有不同路径。参考 [当前 Memory](https://docs.letta.com/agent-sdk/memory)、[MemFS](https://docs.letta.com/concepts/memfs)、[当前依赖与运行时](https://github.com/letta-ai/letta-code/blob/e6d6cc27ef1111da80e919dc1cef179613894b66/package.json)。

**DeepSeek/模型锁定。** 官方 [模型文档](https://docs.letta.com/configuration/models) 支持切换模型和 OpenAI-compatible 自定义提供方，但本轮没有完成 DeepSeek 与当前 App Server 的结构化工具循环验证。可替换模型不代表可低成本替换 harness。现有 Python Gateway/Job/Run 若迁入 Letta，需要明确哪一方拥有重试、权限、上下文、费用与持久化，否则会形成两套运行状态。

**局限和推论，证据中。** 自编辑 persona 与 dreaming 有助连续性，却可能让未经确认的假设进入人格；git 版本可回看，不自动形成每条事实的来源外键与用途权限。git 删除文件通常仍有历史提交，隐私清除须覆盖历史/remote/运行记录。多人物隔离、主人资料与外部发言区分，以及模型读取前的授权仍需应用设计。不能沿用旧 `read_only=True` 示例推断新 MemFS 已满足 FindYou 的批准角色锁定。

**判断。** 只有决定整体采用 Letta harness 时才值得单独评估。当前不为记忆功能重写现有 Python 服务；可借鉴“身份常驻 + 细节按需检索”，但保持主人事实和角色版本由应用控制。

### 4.3 Graphiti / Zep：时间与关系最强相关，但不是零运维记忆库

**已核实，证据高。** Graphiti 是开源 Python 时间知识图框架，Zep 是另一个托管/企业服务产品，不能混算。episode、实体、事实边支持来源与有效时间；`EntityEdge` 有 `episodes/valid_at/invalid_at/expired_at`。它可以支持“该账号当时自述什么、何时改口”的查询结构，抽取结果是否正确仍取决于输入和模型。参考 [概念与依赖](https://github.com/getzep/graphiti/blob/fb60e59d4a208c99e7d0186ee6335d2a257f17f2/README.md)、[边模型](https://github.com/getzep/graphiti/blob/fb60e59d4a208c99e7d0186ee6335d2a257f17f2/graphiti_core/edges.py)。

**存储/服务依赖。** 推荐后端包含 Neo4j、FalkorDB；还有其他驱动。当前 README 已明确 Kuzu 弃用且未来会移除，不推荐把它作为轻量嵌入式首选。FalkorDB Lite 示例也存在 Python/平台前提，本轮未验证 Windows。图库之外仍要 LLM、embedding，某些检索配置还会使用 reranker；选择 Zep Cloud 则是把数据与服务边界进一步外移。[官方 README 后端说明](https://github.com/getzep/graphiti/blob/fb60e59d4a208c99e7d0186ee6335d2a257f17f2/README.md#installation)。

**作用域与权限。** `group_id` 可建立独立 namespace，并限制检索。官方特别说明：应用必须授权 group_id/group_ids，namespace filter 不能代替授权。跨人物查询必须由受信任代码列出允许的 namespace，不能由模型或请求自行指定。[官方 namespacing](https://help.getzep.com/graphiti/core-concepts/graph-namespacing/)。

**删除局限，证据高。** 当前 `remove_episode` 会保留仍被其他 episodes 支持的边、删单独支持的边与孤立节点，并尝试重建剩余实体摘要。但重建异常会记录错误并恢复旧摘要；因此不能承诺一次删除后所有派生文本已清除。还需处理社区/缓存/运行上下文及本地备份，建立污染隔离或重建流程。参考 [当前删除实现](https://github.com/getzep/graphiti/blob/fb60e59d4a208c99e7d0186ee6335d2a257f17f2/graphiti_core/graphiti.py) 的 `remove_episode`，而非仅凭旧博客断言支持或不支持级联删除。

**DeepSeek 可适配，未实测。** 官方 `OpenAIGenericClient` 明确兼容 `/chat/completions`，并提供 `structured_output_mode='json_object'` 给 DeepSeek 等不支持相同 schema 模式的提供方；该模式将 schema 放提示中，仍需验证输出，不可当作约束解码保证。embedding 与 reranker 必须独立配置。[兼容客户端](https://github.com/getzep/graphiti/blob/fb60e59d4a208c99e7d0186ee6335d2a257f17f2/graphiti_core/llm_client/openai_generic_client.py)。

**判断。** 仅在跨实体关系查询成为实际产品核心、SQL/普通召回无法满足固定验收集时试接。图库最好作为可重建派生视图，原文、主体身份映射及权限仍保留 SQLite。图中关系存在不等于现实关系被证实，也不能自动生成好感概率。

### 4.4 SimpleMem：轻量候选，但核心治理契约缺失

**已核实，证据高。** 项目为 MIT；Python 文本路径先把对话压缩为独立记忆，再做语义、关键词与结构化召回。默认 LanceDB，可通过 backend factory 替换存储；LLM client 接受 model/base_url，DeepSeek 兼容接入有代码路径，但未实测。参考 [VectorStore](https://github.com/aiming-lab/SimpleMem/blob/db80b6a7c591e0ea730a058e9f5fc4eb06572299/simplemem/core/database/vector_store.py)、[LLM client](https://github.com/aiming-lab/SimpleMem/blob/db80b6a7c591e0ea730a058e9f5fc4eb06572299/simplemem/core/utils/llm_client.py)。

**明确范围内的缺口，证据高。** 已检查的文本核心 `MemoryEntry` 只有 entry_id、restatement、keywords、timestamp、persons、entities、topic 等；没有本项目必需的 owner_id/subject_id、来源块外键、确认状态或权限字段。`persons` 是抽取的人名，不能当平台 UID 或授权键。VectorStore facade 提供 clear，但没有单条更正/删除/证据级撤销契约。这是对文本核心的结论，不扩大为“整个仓库所有模式都没有删除”。参考 [MemoryEntry](https://github.com/aiming-lab/SimpleMem/blob/db80b6a7c591e0ea730a058e9f5fc4eb06572299/simplemem/core/models/memory_entry.py)。

**判断。** 可借鉴多路检索，暂不接入整个框架。仓库包含文本、多模态、自演化及 MCP 多条路径；全仓 requirements 还包括研究/评测依赖，不能按其体积判断最小安装，也不能直接整体装进现有环境。所谓 lossless 压缩、论文优胜指标只作为作者主张，本轮未复现，不保证中文社交对话的准确性或成本收益。[仓库说明](https://github.com/aiming-lab/SimpleMem/tree/db80b6a7c591e0ea730a058e9f5fc4eb06572299)。

## 5. 收益、成本与替代方案

以下为针对现有代码的工程估计，证据中；时间是单名熟悉项目开发者的有效工作日，不含真实账号、用户评审或完整功能重做，也不构成排期承诺。

| 方案 | 主要收益 | 主要成本/风险 | 粗估验证时间 | 当前可行性 |
|---|---|---|---|---|
| 扩展现有 SQLite + FTS | 最大程度复用来源/确认/版本/删除；单机备份简单 | 仍需实现人物与时效，中文同义表达可能漏召回 | 契约与合成验收 1–2 日；人物薄片参考既有 S2 的 3–5 日 | 高，优先 |
| SQLite 主库 + Mem0 OSS 副本 | 复用语义检索与 provider adapter | 第二存储、embedding 成本、主库/索引同步及删除历史残留 | 隔离合成试接 2–4 日；上线治理另计 | 中，第一备选 |
| SQLite 主库 + 直接向量存储 | 仅补向量召回，管理边界清楚 | 自行做融合/排序；仍需一致性与索引清除 | 2–4 日试接，取决于现有依赖 | 中，与 Mem0 对比 |
| SQLite + Graphiti 派生图 | 时间事实与跨实体检索表达更直接 | 图服务、抽取/消歧/摘要/重建的调用与运维成本 | 4–7 日证明一个受限场景；正式迁移更长 | 中，条件满足再做 |
| 当前 Letta harness | 持续状态、人格、MemFS 和工具循环 | 跨语言/运行时与权限所有权迁移，影响现有 Gateway/Job/Run | 至少单列架构评估 5–10 日，不能视为普通库接入 | 低，不适合本阶段 |
| SimpleMem 文本核心 | 压缩与多路召回较直接 | 主体/证据/生命周期需大量补齐，压缩误差难追溯 | 2–4 日演示；满足治理无可靠估计 | 中低，暂借鉴 |

模型成本没有实测前不报金额。应记录每次摄取/更新/检索的模型调用数、输入输出 tokens、embedding 数与存储量；单条互动可能触发多个抽取/消歧/摘要调用。换聊天模型通常不要求搬迁业务记忆；换 embedding 模型一般需要重建向量，属于真实迁移成本。开源且可自部署会降低托管绑定，但不能消除数据结构、运行时和重建流程的绑定。

## 6. 可落地的最小实施契约

该段为建议，未实施。

1. **主库保持权威。** 在现有模型上追加主体与领域，保留旧 owner 数据映射；独立 Contact/PlatformIdentity，按平台稳定 UID 建池，昵称不是身份键。原始来源、观察时间、事件时间及版本不随摘要覆盖。
2. **分开四条通道。** 主人事实/价值观必须来自本人资料或确认；人物自述允许自动记录为“此人说过”，附源消息；关系推断单列认识类型和失效条件；主人反馈只支持有适用场景、可撤销的表达偏好候选。AI 草稿或他人猜测只可作工作事件，不能单独证明主人经历、立场或现实关系。
3. **稳定人格由应用固定。** 已批准身份/价值与角色版本常驻语境；人物历史按主题检索。表达偏好学习可以自动产生低风险假设，但敏感画像与人格核心变更走确认。角色切换不改变已有事实。此设计降低漂移，不承诺模型绝无偏离。
4. **先做小接口。** `search(query, scope, limit) -> memory_id/version/score`，其中 scope 必须由后端构造，含 owner、subject/domain、model用途、公开范围与有效时间；保留 SQLite 实现。只有接第二存储时再增加幂等 `upsert/delete/rebuild`，不先抽象整个 Agent 框架。
5. **来源与权限先于模型。** 索引摄取只使用明确允许送该模型的数据；禁止把私有全量资料先发云端再事后过滤。返回索引 ID 后再次按主库当前状态核验，旧版本/删除/撤权一律不可用，权限错误不能只依赖模型遵守提示。
6. **异步索引可重建。** 主库事务写事件，再由现有 Job/outbox 式任务处理索引；记录主库版本、索引版本、幂等键和失败状态。删除先在主库阻断检索，派生清除有状态并可重试；旧任务不能复活新删除的数据。
7. **纠正与过期明确。** 纠正保留主人新反馈事件，并记录替代关系；临时近况到期后不可作为现状断言，但历史查询仍可标“当时自述”。过期、撤权、停用与物理清除分开；删除请求明确覆盖原文/向量/摘要/历史/运行快照/备份的不同保留策略。

## 7. 接入验收与升级触发条件

以下必须由固定合成数据先验证，真实数据与费用测试需另在既有授权范围内开展；本轮没有完成这些测试。

- 两个不同 UID 同名，跨视频仍按同 UID 接续；不同对象与不同 owner 均不串记忆。
- 公共回复上下文不含私人资料；禁止送模型的数据也不进入云 embedding/抽取调用。
- “他人说主人已婚”和“AI 自称曾旅行”不能写入主人事实；主人明确纠正可产生新版本，旧版本后续检索不可作为现状使用。
- 对方先说喜欢 A、后来明确改为 B：保留历史与来源，当前检索使用新有效记录；不明冲突返回冲突而非静默选一条。
- 删除唯一来源后，事实、派生摘要与索引不可继续输出；共享来源情况下仅保留仍有合法证据的条目。索引失败、摘要重建失败或旧 Job 重跑不得恢复已撤销材料。
- 一次接受草稿不泛化为稳定人格；重复反馈只能产生带场景的偏好假设，主人能关闭学习、纠正和撤销；外部赞同不直接训练主人事实。
- 每次运行能回溯角色版本、记忆版本、来源与权限决策；换模型保留记忆，结构化失败明确返回，不能静默换模型假装成功。
- 索引重建后合成查询集结果和隔离约束保持；所有新依赖固定版本，网络端点与遥测行为明确。

**升级触发条件（判断，证据中）：** 先积累 30–50 条人工标注的中文查询，涵盖同义表达、时效、纠正、人物隔离与拒答。只有观察到现有检索漏召回且向量方案在同一集合能稳定改善，同时零权限/主体污染，才选择 Mem0 或直接向量库；不能仅凭公开 benchmark 换架构。只有真实需求反复出现多跳/时间关系且普通检索成本明显时，再验证 Graphiti。该小集合是回归门槛，不能证明真实总体精度。

## 8. 结论的适用限度

已核实的是上游当前公开接口/源码及 FindYou 已有约束；未核实 Windows 安装兼容、真实 DeepSeek key 下输出质量、并发负载、长期人格一致性、数据规模阈值及完整隐私清除。当前证据足以支持“保留主库、按需增加可替换派生索引”的架构决定，不足以宣布任何候选在 FindYou 上的性能或质量胜出。
