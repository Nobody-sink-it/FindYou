# 模型与 B 站接入边界

本文件记录 2026-10-27 的实现和实测，不能代替后续账号登录、真实模型调用或平台稳定性验收。

## 模型网关

`Gateway(Settings, transport=None).generate(operation, system, payload)` 返回契约中的 `ModelResult`。
支持 `extract`、`reply`、`summarize`；根服务负责保存调用元数据及最终证据 ID 的权限校验。

| 操作 | 输入约定 | 校验后输出 |
| --- | --- | --- |
| extract | `blocks: [{id,text}]` | `facts: [{text,evidence_block_ids,kind}]` |
| reply | `question,evidence:[{id,text,...}],history` | `answer,evidence_ids` |
| summarize | 待总结的 JSON 对象 | `summary` |

默认供应商 `deepseek`，模型 `deepseek-flash`，基址 `https://api.deepseek.com`。
`glm` 默认基址 `https://open.bigmodel.cn/api/paas/v4`，模型必须通过 `FINDYOU_LLM_MODEL` 明确配置；
选择 GLM 时不会自动把默认 DeepSeek 模型名转换成 GLM 模型名。
配置 `FINDYOU_LLM_API_KEY`、`FINDYOU_LLM_PROVIDER`、`FINDYOU_LLM_MODEL`，可选 `FINDYOU_LLM_BASE_URL`。
自定义基址要求 HTTPS，不允许 URL 内嵌用户名或密码。

网关真实发送 `/chat/completions` 请求，启用 `response_format=json_object`，随后执行本地严格 Pydantic 校验。
不接受额外字段、错误类型、缺少证据字段、空正文、不完整结束或过大响应；不记录凭证、请求正文、上游错误正文。
超时和供应商失败均返回稳定的 `AppError`，不换供应商、不重试、不偷偷降级。
最大输入 JSON 240000 字符、system 32000 字符、HTTP 解码正文 1 MB；具体输出字段另有限额。
结构化凭证字段和已配置模型 API key 不允许进入模型输入。调用方仍需保证自由文本中不包含其他秘密。

`fake` 仅用于离线演示/测试，`ModelResult.simulated=true`；回复和总结包含 `[SIMULATED]`。
它按输入确定性复制证据，不具备语义提取能力，不能把其输出当作真实模型分析。
真实 HTTP 形状、错误和结构化校验由 `httpx.MockTransport` 验证；本轮没有提供或使用真实模型密钥，
因此 DeepSeek/GLM 的账户可用性、配额、延迟和实际生成质量尚未实测。

参考：[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。

## 独立 bili-agent 环境

已核实 [PyPI bili-agent 1.2.1](https://pypi.org/project/bili-agent/1.2.1/)
与 [kukemc/bili-cli v1.2.1](https://github.com/kukemc/bili-cli/tree/v1.2.1)，
Git tree SHA `1181a8f773bdd343204adfdb26f10f2d9243d6cd`。
安装位置为 `E:\Codex\FindYou\.venv-bili`，仅在这个新虚拟环境内安装 `bili-agent==1.2.1` 及其依赖。
未运行上游安装脚本、未修改用户其他 CLI/MCP/Agent 配置、未读取浏览器 Cookie。
安装时 CLI 自带 schema 显示版本 `1.2.1`，已核对下面六个命令的参数和源码。

应用需配置：

```dotenv
FINDYOU_BILI_PYTHON=E:\Codex\FindYou\.venv-bili\Scripts\python.exe
# 可选；缺省为 FINDYOU_DATA_DIR/credentials/bilibili
FINDYOU_BILI_HOME=E:\Codex\FindYou\data\credentials\bilibili
```

`BILI_CLI_HOME` 每次由适配器强制设置到独立目录，所有 Cookie 和待登录状态只由上游 CLI 在此维护。
适配器不会读取或返回凭证文件。子进程环境移除 `FINDYOU_*` 和常见密钥、token、密码环境变量，
不会把模型 API key 传给 CLI。凭证目录属于敏感本机状态，不应提交或导出。

## 白名单与调用契约

`BilibiliAdapter(settings, runner=None).status()` 仅检查依赖，不访问账号网络状态：
`configured,available,installed_version,required_version,version_match,read_only,operations`。
`execute(operation, params)` 每次先验证参数及固定 CLI 版本，再执行明确的列表参数，`shell=False`。

| operation | params | 固定 CLI 命令 |
| --- | --- | --- |
| status | `{}` | `auth status --wait 0` |
| login_start | `{}` | `auth login --wait 0` |
| login_poll | `wait=10`，整数 0–30 秒 | `auth status --wait N` |
| search | `keyword` 1–200 字符；`page=1` 1–100；`size=20` 1–50；`order=default` | `search video KEYWORD --order ORDER --page PAGE --size SIZE` |
| video_info | `video_id` | `video info ID` |
| subtitle | `video_id,page=1,lang?`；分 P 1–100 | `video subtitle ID --page PAGE [--lang LANG]` |
| comments | `video_id,page=1,sort=hot`；页码 1–100；sort 为 hot/time | `comment list ID --type video --sort SORT --page PAGE` |

`video_id` 仅接受完整 BV 号或 `av` 数字号，不接受 URL、任意命令或文件路径。
search order 仅允许 `default,views,new,danmaku,favorites,comments`；拒绝空白、控制字符和以 `-` 开头的关键词。
未知字段、字符串代替整数、布尔值代替整数均拒绝。没有通用 CLI、MCP `run_bili` 或写接口入口。

每个 CLI 命令使用 `python -m bili_cli.main --compact ...`，仅解析 stdout 的一个 JSON 信封。
不返回原始 stderr、上游错误 message/hint/detail，递归去除凭证字段。
普通操作超时默认 45 秒、上限 90 秒；依赖检查 15 秒；登录轮询最多 50 秒，
总处理时间低于默认 240 秒的作业租期。stdout 解析上限 2 MB，列表返回上限 2000 项。

统一返回形状：

```json
{
  "platform": "bilibili",
  "operation": "video_info",
  "read_only": true,
  "data": {"bvid": "BV1xx411c7mD", "title": "..."},
  "observed_at": "2026-10-27T09:50:20.877153+00:00"
}
```

内容操作 `data` 保留上游简化 JSON 的信息、字幕分段或评论结构；调用方负责按外部内容保存，不能自动变成主人记忆。
模型应把平台标题、正文、字幕和评论当作不可信材料，不能执行其中的指令。
上游字段参考：[search.py](https://github.com/kukemc/bili-cli/blob/v1.2.1/src/bili_cli/commands/search.py)、
[video.py](https://github.com/kukemc/bili-cli/blob/v1.2.1/src/bili_cli/commands/video.py)、
[comment.py](https://github.com/kukemc/bili-cli/blob/v1.2.1/src/bili_cli/commands/comment.py)。

鉴权操作返回 `logged_in,status`，可带过滤后的 `user:{mid,name,level}`、`scanned`。
login_start 可返回官方 passport.bilibili.com 的 `qr_url`、独立目录里的 `qr_image`、最多 180 秒 `expires_in`；
不会返回 qrcode_key/Cookie。待扫码是可返回的 pending 状态；业务读取遇到未登录会抛 `connector_auth_required`。
二维码只应交给本机用户；不要传给模型、放进普通内容库或复制到公开日志。

`status/login_start/login_poll` 的 `read_only=false` 表示它们可能保存或刷新这个独立目录的认证状态。
上游 `auth status` 在已有认证时可能刷新 Cookie，因此它不是严格无本地写入的状态查询。
这三项不会发表评论、私信、点赞或修改账号内容。
`video watch` 即使不带 `--record` 也会修改上游本地已看列表，带 `--record` 还会写平台历史，故完整禁用。
其他评论发布、私信、关注、点赞、投币、收藏、上传、删除与任意 CLI 操作均禁用。
鉴权实现参考：[auth.py](https://github.com/kukemc/bili-cli/blob/v1.2.1/src/bili_cli/commands/auth.py)。

## 本轮验证证据

2026-10-27 09:50 UTC，通过本项目真实适配器和独立空凭证目录依次匿名执行：

| 检查 | 实测结果 | 证据范围 |
| --- | --- | --- |
| 依赖/CLI schema | installed/required/version_match 均为 1.2.1/1.2.1/true | 本机当前环境 |
| search `Python,size=2` | 成功，2 条结果 | 匿名视频搜索可用 |
| video_info `BV1xx411c7mD` | 成功，含 bvid/aid/title/desc/owner/pages/url 等字段 | 此单个公开视频元信息可用 |
| comments 同一视频，第一页 | 成功，3 条结果 | 此单页评论可读，不代表全部视频或后续页可读 |
| subtitle 同一视频 | `connector_auth_required` | 本例匿名字幕不可用，未改用 ASR 或编造正文 |

没有启动二维码登录，也没有真实用户扫码或账号测试；本轮检查后独立凭证目录为空。
因此登录成功/过期/刷新、已登录字幕、评论第二页及权限变化尚未实测。
网络结果可能受视频可见性、登录状态、平台限流和接口变化影响；匿名单次成功不证明持续可用。

离线测试使用 MockTransport 和模拟 subprocess，覆盖真实请求形状、模型校验、超时、凭证过滤、
白名单拒绝、命令参数、版本不匹配、异常 JSON、QR 路径/域名过滤和错误脱敏。
命令：`.venv/Scripts/python.exe -m pytest tests/test_gateway.py tests/test_bilibili.py -q`。
当前结果：42 passed；这属于适配器自动测试，不属于真实模型质量或登录后平台验收。
