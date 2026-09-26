# 后端模块设计

## 1. 工程结构

推荐后端目录：

```text
backend/
  app/
    api/                 # FastAPI routers 与依赖
    core/                # 配置、日志、安全、错误码
    db/                  # SQLAlchemy、Alembic、Repository
    models/              # ORM 模型
    schemas/             # Pydantic 请求/响应
    services/            # 业务服务
    integrations/
      chatwoot/          # Application API Client
      ai/                # Mock、HTTP、LangGraph 协议
      notifications/     # 后续邮件/企业微信适配器
    workers/             # 事件、AI、SOP、对账处理器
    main.py              # API 入口
    worker_main.py       # Worker 入口
  alembic/
  tests/
  pyproject.toml
  .env.example
```

依赖基线：FastAPI、Uvicorn、SQLAlchemy 2、Alembic、Pydantic Settings、httpx、Argon2 密码库、cryptography、python-multipart、pytest、pytest-asyncio 和 respx。LangGraph 作为可选依赖，不进入首期业务执行路径。

## 2. 模块职责

| 模块 | 职责 | 主要输入/输出 |
|---|---|---|
| Auth/RBAC | 初始化管理员、登录、Session、CSRF、角色与 Inbox 范围 | Cookie、UserContext |
| Chatwoot Connection | 加密保存连接、只读测试、资源同步 | Account、Inbox、Agent、Team、Label |
| Webhook Gateway | 路由密钥、签名、大小限制、幂等入库、快速响应 | webhook_events |
| Event Processor | 解析 Chatwoot 事件并更新本地镜像 | contacts、conversation_states、message_events |
| Conversation Control | 计算最终 AI 状态、标签合并、阶段和人工状态 | EffectiveAiState |
| AI Orchestrator | 上下文构建、Adapter 调用、动作校验、发送前复核 | ai_runs、outbound_messages |
| Handoff | 创建/领取/改派/完成/恢复、Chatwoot 分配 | handoff_tasks |
| SOP | 策略版本、发布校验、入组、调度、频控和退出 | sop_jobs、outbound_messages |
| Contacts/Lead | 联系方式识别结果保存、确认、脱敏、同步 | lead_captures、contacts |
| BI | 事件口径、聚合和查询 | daily_metrics |
| Audit | 关键变更的操作者、前后值和请求 ID | audit_logs |
| Health | DB、Worker 心跳、Chatwoot 与 AI 连接状态 | health response |

Router 只负责协议、认证、参数校验和响应转换。业务规则位于 Service；SQL 位于 Repository；外部 HTTP 位于 Integration Client。

## 3. 配置和密钥

`.env.example` 只声明变量：

```dotenv
APP_ENV=development
APP_SECRET_KEY=
APP_ENCRYPTION_KEY=
DATABASE_URL=sqlite:///./data/app.db
FRONTEND_ORIGIN=https://ai.luoxuecong.asia
SESSION_COOKIE_DOMAIN=.luoxuecong.asia
SESSION_COOKIE_SECURE=true
SESSION_TTL_HOURS=12
CHATWOOT_REQUEST_TIMEOUT_SECONDS=15
AI_REQUEST_TIMEOUT_SECONDS=15
WORKER_POLL_INTERVAL_SECONDS=1
WORKER_LEASE_SECONDS=60
UPLOAD_DIR=./data/uploads
MAX_UPLOAD_MB=25
LOG_LEVEL=INFO
```

Chatwoot Token 和 AI Token 使用 `APP_ENCRYPTION_KEY` 加密后存入数据库。更新 Token 时只返回 `configured=true`、末四位和版本，不返回明文。日志过滤 `api_access_token`、`Authorization`、Cookie、CSRF、邮箱、电话和 WeChat 完整值。

## 4. 登录与 RBAC

### 4.1 Session

- 密码使用 Argon2id 哈希。
- 登录成功生成 256 bit 随机 Session ID，数据库仅保存其 SHA-256 哈希。
- Cookie 名为 `aiops_session`，设置 `HttpOnly; Secure; SameSite=Lax; Domain=.luoxuecong.asia; Path=/`。
- 每个 Session 生成 CSRF Secret；前端从 `/v1/auth/csrf` 获取 token，并在写请求发送 `X-CSRF-Token`。
- 写请求同时校验 `Origin=https://ai.luoxuecong.asia`。
- CORS 只允许该 Origin 且 `allow_credentials=true`，不允许 `*`。

### 4.2 角色

| 角色 | 数据范围 | 核心权限 |
|---|---|---|
| `super_admin` | 全租户 | 密钥、连接、用户、全部策略与数据 |
| `operations_admin` | 授权 Inbox | AI/SOP、标签映射、人工队列、BI |
| `supervisor` | 授权 Team/Inbox | 会话、领取/改派、恢复 AI、团队 BI |
| `agent` | 本人及未分配 | 会话查看、领取、备注、完成任务 |
| `bi_viewer` | 授权聚合范围 | 只读 BI，联系人默认脱敏 |

后端每个资源查询都注入 `UserContext`，先限制 tenant，再限制 Inbox/Team/Assignee。前端隐藏按钮只是体验，不能代替后端权限校验。

### 4.3 首次初始化

提供 CLI：

```text
python -m app.cli create-admin --email admin@luoxuecong.asia
```

密码通过交互式输入，不允许命令行参数或日志输出。没有用户时，除 `/health` 和 CLI 外，不开放浏览器自助注册。

## 5. Webhook Gateway

接收路径：`POST /v1/webhooks/chatwoot/{connection_key}`。

处理顺序：

1. 按 `connection_key` 查找启用连接，未知路径返回 `404`。
2. 限制 Body 大小，首期 2 MB；附件本体不会包含在 Webhook JSON。
3. 读取原始 Body，若配置 Secret，则验证 Chatwoot 时间戳与 HMAC 签名。
4. 解析 JSON，校验 `event`、`account.id` 与绑定 Account。
5. 计算资源 ID：消息事件用顶层 `id`，会话事件用会话 ID，联系人事件用联系人 ID。
6. 使用唯一键插入 `webhook_events`，重复时不创建新任务。
7. 提交事务后返回 `202 {"accepted": true}`。

签名不可用时采用不可预测路径、HTTPS、请求速率限制、Account 校验和业务幂等；Cloud 支持签名后必须启用。

## 6. Worker 和任务租约

Worker 循环处理 `webhook_events` 与 `sop_jobs`：

```text
BEGIN IMMEDIATE
  查询 status=pending 且 available_at<=now 的一条记录
  条件更新 status=processing, lease_owner, lease_expires_at, attempts+1
COMMIT
执行外部调用
短事务写入结果
```

规则：

- 租约过期的 `processing` 任务可重新领取。
- Webhook 事件最多 8 次，指数退避上限 15 分钟；确定性数据错误直接 `dead`。
- AI 调用最多 2 次；Chatwoot 发送仅在确定未提交时重试。
- 同一 `conversation_id` 用 `conversation_states.processing_lease_*` 获取会话租约。
- 外部 HTTP 调用期间不持有 SQLite 事务。
- Worker 每 10 秒写心跳，Health 超过 30 秒未更新显示异常。

## 7. Chatwoot Client

统一基础路径：`{base_url}/api/v1/accounts/{account_id}`，请求头使用服务端 `api_access_token`。

Client 方法：

- `test_connection()`、`list_inboxes()`、`list_agents()`、`list_teams()`、`list_labels()`。
- `get_conversation()`、`list_conversation_messages()`。
- `get_conversation_labels()`、`replace_conversation_labels()`。
- `get_contact_labels()`、`replace_contact_labels()`、`update_contact()`。
- `assign_conversation()`、`toggle_conversation_status()`、`update_custom_attributes()`。
- `create_text_message()`、`create_attachment_message()`、`get_message()`。
- `list_webhooks()`、`create_webhook()`、`update_webhook()`。

所有方法统一返回业务 DTO，不让 Chatwoot 原始结构穿透到 Router。错误分为 `unauthorized`、`forbidden`、`not_found`、`rate_limited`、`channel_blocked`、`validation_error`、`temporary_failure`。

标签写入必须 GET 当前完整列表、合并目标变化、POST 完整列表。未知人工标签永远保留。

## 8. AI Adapter

统一协议：

```python
class AiAdapter(Protocol):
    async def generate_reply(self, context: AiContext) -> AiDecision: ...
```

`AiContext` 至少包含 tenant、Inbox、Conversation、Contact、触发消息、最近历史、标签、阶段和可执行能力。`AiDecision` 只允许：

- `reply`：包含一条文本回复。
- `handoff`：包含标准原因、说明和建议团队。
- `no_action`：说明跳过原因。
- `update_stage`：提交候选阶段，可同时回复。
- `capture_contact`：提交类型、值和置信信息，默认待确认。

首期实现：

- Mock：配置触发文本与固定回复；用于端到端测试。
- HTTP：POST 到企业接口，Bearer/API Key 仅后端使用；15 秒超时，最多 2 次临时错误重试。
- LangGraph：只提供实现同一 Protocol 的扩展入口，不创建旅游业务节点。

AI 返回内容必须通过长度、空值、动作枚举和当前渠道能力校验。AI 不能直接操作数据库或 Chatwoot。

## 9. 人工接管

创建接管任务时，在同一事务内：

1. 将本地状态改为 `HUMAN_HANDOFF` 并增加状态版本。
2. 取消未提交的 AI/SOP 任务。
3. 创建唯一的活跃 `handoff_task`。
4. 写审计记录。

事务后调用 Chatwoot：合并 `人工接管`/`客诉` 等标签并分配团队或坐席。外部同步失败不恢复 AI，而是记录 `sync_failed` 并重试。

领取任务使用版本号条件更新；冲突返回 `409 handoff_already_claimed`。完成任务只关闭队列项，不恢复 AI。恢复 AI 是独立操作，需要删除阻止标签、重新计算状态并记录操作者。

## 10. SOP 与媒体

策略发布时生成不可变版本。运行中的 enrollment 固定引用版本，不受草稿修改影响。

支持节点：等待、固定时间、条件、发送消息、更新阶段、转人工、结束。首期发送消息支持文本和图片 P0，音频/视频/文件 P1。

媒体先上传到 `data/uploads`，记录 SHA-256、MIME、大小和原文件名。发送时后端读取文件并以 `multipart/form-data` 的 `attachments[]` 上传 Chatwoot。禁止让浏览器传任意服务器路径，禁止把 Chatwoot 临时 URL 当作长期媒体源。

## 11. BI 与审计

Chatwoot Reports 仅用于对账。AI 接管率、转人工、SOP 回复、留资和漏斗来自本平台事件表。聚合任务按租户时区每日重算最近 3 天，允许迟到事件修正。

审计覆盖：登录失败、连接/Token 变更、全局与 Inbox 开关、会话 AI、阶段、转人工、恢复、SOP 发布/暂停、用户权限和敏感信息查看。审计日志只追加，不提供普通 API 删除。

