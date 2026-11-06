# S0/S1 施工契约

2026-10-30。第一批施工范围：真实视频采集任务、规范视频/讨论/评论与账号观测、可分页只读数据台。人物记忆/角色草稿/关系评估/Run归因在后续阶段实施，不以原型数据充数。

统一前缀 `/api/v1`，Bearer Token认证。ID全部字符串，日期UTC ISO8601（带Z）；页面转本地时间。分页 `{items,total,limit,offset}`，limit 1–100，offset>=0。未知为null，未获取不作0；错误使用既有 `{error:{code,message,retryable}}`。

## 新接口

- `POST /videos/capture` body `{video_id: "BV..." 或规范B站视频URL, page:1, sort:"hot"|"time", include_subtitle:false}` → 202，既有Job结构。一个任务读取详情与指定页评论，可选字幕；不抓无限分页，不发模型调用、不发布。返回任务result `{video_id:内部ID, thread_ids:[], content_ids:[], coverage:{...}}`。
- `GET /videos?limit=20&offset=0&q=...&sort=observed_desc|observed_asc` → VideoPage。
- `GET /videos/{id}` → Video。
- `GET /threads?video_id=内部ID&q=...&limit=20&offset=0&sort=observed_desc|observed_asc` → ThreadPage。
- `GET /threads/{id}` → Thread，加 `video:Video` 与 `root_message:Message|null`。
- `GET /threads/{id}/messages?limit=50&offset=0&parent_id=可选平台消息ID` → MessagePage。默认分页当前线程所有已获取消息，带回复边；前端平铺按根/父引用组织，不声称完整树。parent_id用于限定直属已获取回复。
- `GET /console/summary` → `{videos,threads,messages,identities,platform_write:false}`，均为当前owner本地去重观测量，不是平台总量。

## 对象

Video: `{id,platform,external_id,title,author_name,author_uid,source_url,description,reported_comment_count:null|int,collected_message_count:int,collected_thread_count:int,participant_count:int,last_observed_at,coverage:object}`。

Thread: `{id,video_id,root_external_id,root_message_id:null|string,title,author_name,author_uid,reported_reply_count:null|int,collected_message_count:int,participant_count:int,last_observed_at,coverage:object}`。没有真实发送就不产生直接回应AI计数或关系状态。

Message: `{id,video_id,thread_id,external_id,root_external_id,parent_external_id:null|string,reply_to_uid:null|string,author_uid:null|string,author_name,body,posted_at:null|string,first_observed_at,last_observed_at,source_content_id,source_snapshot_id,likes:null|int,reported_reply_count:null|int,is_root:bool,parent_missing:bool,raw:object}`。source_content_id及snapshot允许null只在来源不可映射时；规范化入口要求已存在真实来源。当前消息被后续采集更新时保留旧快照引用历史。

coverage包括 `status:"partial"`、`observed_at`、`requested_page`、`sort`，可附字幕状态；不声称采集全部。同视频重复采集Upsert，重复预览消息只计一次；根/父ID只在可靠字段支持时规范化。一次未观测到不等于删除。

## 后端集成契约

`SocialStore(settings, db)` 位于 `findyou/social.py`，ORM位于 `social_models.py`。
方法：`normalize(operation, data, video_external_id, content_id, observed_at=None, coverage=None) -> dict`；只接 `video_info/comments/subtitle`，返回 `{video_id,thread_ids,coverage}`。`list_videos(limit=20,offset=0,q=None,sort="observed_desc")`、`get_video(id)`、`list_threads(limit=20,offset=0,video_id=None,q=None,sort="observed_desc")`、`get_thread(id)`、`list_messages(thread_id,limit=50,offset=0,parent_id=None)`、`summary()`。缺失/非owner数据404。public API不返回凭证、原始文件路径。

主代理独占数据库迁移、runtime/worker/API/CLI集成；执行者只写明确分配文件。前端在 `frontend/`，Vite开发代理转发 `/api` 和 `/health` 到127.0.0.1:8765，不开启通配CORS。Token仅内存持有，刷新重新认证，绝不写入浏览器存储、URL、日志。生产构建由后端同源静态挂载，缺构建明确提示。
