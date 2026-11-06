# FindYou

基于可追溯个人记忆的 AI 社交 Agent，当前版本 **0.1.0**。
提供人生访谈、资料录入、自传和检索工作区，以及 B 站只读发现、评论语境和回复草稿。

人生工作区使用同源 `/life/`，当前数据库迁移为
`0004_life_model`。本机安装 Node.js 24 后，在项目根目录执行 `npm --prefix frontend ci` 和
`npm --prefix frontend run build` 生成页面，再按下方命令启动 API 与 Worker。
迁移已有个人数据前先使用 `backup`，再运行 `init-db`。
页面使用本机管理令牌登录，访谈回答的云处理默认关闭，可逐条授权。
模型调用会读取 [访谈员工作手册](src/findyou/life_editorial.md)。

## Windows 启动

需要 Python 3.13（64 位）。在项目目录运行：

```powershell
.\scripts\bootstrap.ps1
.\.venv\Scripts\python.exe -m findyou.cli doctor
```

脚本建立独立环境，下载官方 SQLite 3.51.3 DLL，仅让项目 `.venv` 使用；
不会替换系统 Python / Anaconda 的文件。默认 WAL 不接受旧 SQLite。
`DELETE` 仅是显式测试/兼容选项，不会自动降级。

本地 `.env` 的管理 Token 自动生成且不打印。将模型 Key 写入
`FINDYOU_LLM_API_KEY`，不要提交到 Git 或发到聊天。
切换 GLM 时同时设置 `FINDYOU_LLM_PROVIDER=glm` 与对应的 `FINDYOU_LLM_MODEL`。
修改 `.env` 后重启 API 与 Worker；Docker 使用 `docker compose up -d --force-recreate`。

分别在两个终端运行：

```powershell
.\.venv\Scripts\python.exe -m findyou.cli serve
.\.venv\Scripts\python.exe -m findyou.cli worker
```

服务地址 `http://127.0.0.1:8765`；接口说明 `http://127.0.0.1:8765/docs`。
管理接口需要 `.env` 中的 Bearer Token。CLI 直接管理本地数据库，无需 HTTP Token。
查询状态、暂停和恢复也可用 `doctor` / `pause` / `resume`。

## 最小资料记忆闭环

```powershell
# 导入默认为私有；加 --public 表示允许作为公开回复证据。
.\.venv\Scripts\python.exe -m findyou.cli import .\my-profile.md --public
.\.venv\Scripts\python.exe -m findyou.cli worker --once
.\.venv\Scripts\python.exe -m findyou.cli content
.\.venv\Scripts\python.exe -m findyou.cli extract <content_id>
.\.venv\Scripts\python.exe -m findyou.cli worker --once
.\.venv\Scripts\python.exe -m findyou.cli memories
.\.venv\Scripts\python.exe -m findyou.cli confirm <memory_id>
.\.venv\Scripts\python.exe -m findyou.cli conversation --peer alice --thread video-1
.\.venv\Scripts\python.exe -m findyou.cli chat <conversation_id> "你在开发什么项目？"
```

导入和提取是两个任务。模型只能提议候选事实，确认后才参与问答。
纠正会新建版本并重新要求确认；公开问答同时要求记忆及全部原始证据公开。
历史生成回复不作为新的个人事实。没有合格资料时明确答复“不足以判断”。
同义词理解受关键词检索限制；向量检索后置。

统一 JSON 导入为 `ImportRequest` 对象，例如：

```json
{"title":"我的项目","text":"我正在开发 FindYou。","format":"text","platform":"local","visibility":"private","authorship":"owner","source_url":null,"metadata":{}}
```

`source_url` 只保存出处，不自动访问。文字、Markdown 可上传或通过 CLI 导入。
图片、音频、视频有结构预留，OCR / ASR / VLM 处理未启用，接口会如实报告能力。

## B站只读

`.venv-bili` 固定安装 `bili-agent==1.2.1`，独立凭证目录默认为
`data/credentials/bilibili`。只开放登录、状态、搜索、详情、字幕和评论。
参数与返回结构见 [连接器说明](docs/CONNECTORS.md)。

```powershell
.\.venv\Scripts\python.exe -m findyou.cli bili search '{"keyword":"Python","size":2}'
.\.venv\Scripts\python.exe -m findyou.cli worker --once
.\.venv\Scripts\python.exe -m findyou.cli jobs
```

需要登录时运行 `bili login_start` 并处理一次任务；从该任务结果中的
`qr_image` 打开二维码，用自己的手机确认，再运行 `bili login_poll`。
二维码和凭证仅在本机。登录接口可能刷新登录态，这是账号接入操作；内容读取不使用
会写入观看历史的 `watch` 命令。没有登录时，部分公开读取可用，字幕可能要求登录。

## Docker Compose

```powershell
docker compose build
docker compose up -d
```

先创建 `.env` 并设置管理 Token 与模型配置。镜像编译 SQLite 3.51.3，API 与 Worker
共用名为 `findyou-data` 的 Docker 卷。宿主端口只绑定本机；容器内部监听所有接口。
容器数据和 Windows 本机 `data/` 是两套独立环境，凭证、账号登录状态不会自动共享。
不要同时在不同宿主机或网络文件系统上使用同一 SQLite 文件。

## 测试与验证边界

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m findyou.cli backup .\backups\findyou.zip
```

恢复必须停掉相关进程，选择空的 `FINDYOU_DATA_DIR`，再运行 `restore <zip>` 和 `init-db`。
备份不包含模型 Key、管理 Token 或 B站凭证；恢复后单独配置并登录。

离线测试使用显式 `fake` 模型和模拟子进程，不证明真实模型或平台可用。
真实验证记录见 [验证记录](docs/VALIDATION.md)。模型调用只记录元数据与用量，不保存 Key、
完整 Prompt 或平台 Cookie。模型版本与 token 用量可查，当前不估算金额。

## 下一阶段

可从人生工作区录入资料、续访、确认记忆和编辑自传。B 站任务支持有限的只读发现与草稿，
可选的公开 README 摘录支持预览和导出。后续工作包括独立社交控制台、发布执行和新回复跟进。

源码采用 MIT；第三方说明见 [NOTICE.md](NOTICE.md)。请勿提交个人资料、凭证或数据库。
