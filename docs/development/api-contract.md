# 前后端 API 契约

## 1. 通用协议

Base URL：`https://api.luoxuecong.asia/v1`。除 Webhook 和 Health 外均需 Session Cookie。所有写请求需 `X-CSRF-Token`。

成功响应直接返回资源；列表格式：

```json
{
  "items": [],
  "page": 1,
  "page_size": 25,
  "total": 0
}
```

错误格式：

```json
{
  "error": {
    "code": "conversation_version_conflict",
    "message": "会话状态已变化，请刷新后重试",
    "field_errors": {},
    "request_id": "req_..."
  }
}
```

通用状态码：`400` 业务参数、`401` 未登录、`403` 无权限、`404` 不存在或不可见、`409` 并发冲突、`422` Schema 校验、`429` 限流、`502` 外部系统失败、`503` 服务暂不可用。

## 2. Auth

| 方法与路径 | 请求/响应 | 权限 |
|---|---|---|
| `POST /auth/login` | `{email,password}` -> CurrentUser；设置 Cookie | 公开，限流 |
| `POST /auth/logout` | 无 -> `204`；撤销 Session | 已登录 |
| `GET /auth/me` | CurrentUser、roles、inbox_scopes | 已登录 |
| `GET /auth/csrf` | `{csrf_token, expires_at}` | 已登录 |

CurrentUser：

```json
{
  "id": 1,
  "email": "admin@luoxuecong.asia",
  "display_name": "管理员",
  "roles": ["super_admin"],
  "permissions": ["settings.manage", "conversation.handoff"],
  "inbox_scopes": [{"inbox_id": 128859, "scope": "all"}]
}
```

登录错误统一 `401 invalid_credentials`；停用账号返回 `403 account_disabled`。

## 3. Chatwoot 与系统设置

### 3.1 连接

| 方法与路径 | 行为 | Chatwoot 映射 | 权限 |
|---|---|---|---|
| `GET /settings/chatwoot` | 返回脱敏连接状态 | 无 | Super Admin |
| `PUT /settings/chatwoot` | 保存 base_url/account/token | 后台验证 URL，不立即发消息 | Super Admin |
| `POST /settings/chatwoot/test` | 只读连接测试 | `GET /inboxes` | Super Admin |
| `POST /settings/chatwoot/sync` | 同步资源 | Inboxes、Agents、Teams、Labels、Inbox Members | Operations Admin+ |

保存请求：

```json
{
  "base_url": "https://app.chatwoot.com",
  "account_id": 180474,
  "api_token": "仅在新增或轮换时提交"
}
```

响应只包含 `token_configured`、`token_last4`、`token_version`、`status` 和测试时间。

### 3.2 Inbox 策略

| 方法与路径 | 请求/响应 | 权限 |
|---|---|---|
| `GET /settings/inboxes` | InboxBinding 列表 | 授权用户只读自己的范围 |
| `PATCH /settings/inboxes/{id}` | `{ai_enabled,sop_enabled,default_team_id,version}` | Operations Admin+ |
| `PATCH /settings/tenant-policy` | `{ai_enabled,sop_enabled,version}` | Super Admin |

关闭策略只影响本平台自动动作，不修改 Chatwoot Inbox，也不影响人工回复。

### 3.3 Account Webhook

| 方法与路径 | 行为 | Chatwoot 映射 |
|---|---|---|
| `GET /settings/chatwoot/webhook` | 返回 URL、事件、webhook_id、最近接收 | `GET /webhooks` 对账 |
| `PUT /settings/chatwoot/webhook` | 创建或更新账号 Webhook | `POST /webhooks` 或 `PATCH /webhooks/{id}` |
| `POST /settings/chatwoot/webhook/verify` | 检查配置与最近真实事件 | 不模拟客户消息 |

请求事件必须属于允许集合，`message_created`、`message_updated`、`conversation_updated`、`conversation_status_changed`、`contact_updated` 不可移除。

### 3.4 AI Adapter

| 方法与路径 | 行为 | 权限 |
|---|---|---|
| `GET /settings/ai` | 返回 adapter、endpoint、超时、密钥状态 | Operations Admin+ |
| `PUT /settings/ai` | 保存 Mock/HTTP 配置和密钥版本 | Super Admin |
| `POST /settings/ai/test` | 使用示例上下文测试，不发客户消息 | Operations Admin+ |

HTTP Adapter 配置：`endpoint`、`auth_type`、可选 secret、`timeout_seconds`、`max_retries<=2`。Mock 配置使用触发文本、固定回复和图片测试媒体 ID。

### 3.5 标签与人工规则

- `GET/PUT /settings/label-mappings`：系统语义到 Chatwoot 标签映射。
- `GET/PUT /settings/handoff-rules`：原因、优先级、默认团队和 SLA。
- 保存时调用 `GET /labels` 校验，不自动删除或重命名 Chatwoot 现有标签。

## 4. Webhook

### `POST /webhooks/chatwoot/{connection_key}`

公开但受连接密钥、签名、Account 校验、Body 限制与速率限制保护。请求体为 Chatwoot 原始 JSON。

响应：

```json
{"accepted": true, "duplicate": false}
```

- 新事件：`202`，`duplicate=false`。
- 重复事件：`202`，`duplicate=true`。
- 未知 connection：`404`。
- 签名失败：`401`。
- Account 不匹配：`403`。
- Body/JSON 不合法：`400`。

该接口绝不等待 AI 或 Chatwoot 出站完成。

## 5. Conversations

### 5.1 列表与详情

`GET /conversations?page=1&page_size=25&q=&inbox_id=&ai_state=&journey_stage=&assignee_id=&labels=`

摘要包含：Conversation/Contact ID、联系人脱敏信息、Inbox、渠道、最后消息、AI 最终状态与原因、阶段、标签、团队/坐席、`can_reply`、更新时间。

`GET /conversations/{id}` 返回：摘要全部字段、local override、版本、Contact 标签、Custom Attributes、Chatwoot URL、联系方式权限状态、活跃人工任务、待执行 AI/SOP 数量和同步健康。

后端先按 RBAC 限制 Inbox，再读取资源。无权资源返回 `404 resource_not_found`，避免泄漏存在性。

### 5.2 消息历史

`GET /conversations/{id}/messages?before=&limit=50`

本地已有记录优先返回；缺页时由后端调用 Chatwoot `GET /conversations/{id}/messages` 并更新镜像。响应标记 `source=local|chatwoot`、消息方向、归因、附件和送达状态。

### 5.3 AI 与阶段

`PATCH /conversations/{id}/ai-state`

```json
{"override": "enabled", "version": 7, "reason": "人工恢复"}
```

`override` 为 `inherit/enabled/disabled`。即使设为 enabled，强阻断仍可令 `effective_ai_state` 关闭。版本不一致返回 `409 conversation_version_conflict`。

`PATCH /conversations/{id}/journey-stage`

```json
{"journey_stage": "quoted", "version": 7}
```

本地保存后调用 Chatwoot Conversation Custom Attributes。响应包含 `local_status` 和 `chatwoot_sync_status`，同步失败不回滚已审计的本地事实。

### 5.4 同步、备注和标签

| 方法与路径 | 行为 | Chatwoot 映射 |
|---|---|---|
| `POST /conversations/{id}/sync` | 拉取详情、标签、分配和最近消息 | Conversation Details、Labels、Messages |
| `POST /conversations/{id}/notes` | 保存平台运营备注 | 默认本地；`sync_private=true` 时 Create private message |
| `POST /conversations/{id}/system-labels` | 增删允许的系统标签 | GET Labels + merge + POST Labels |

系统标签请求：`{add:["人工接管"],remove:["AI处理中"],version:7}`。未知标签不能通过该接口删除。

### 5.5 转人工和恢复

`POST /conversations/{id}/handoff`

```json
{
  "reason_code": "customer_requested_human",
  "reason_detail": "客户要求确认预算",
  "priority": "P1",
  "target_team_id": 42,
  "version": 7
}
```

本地事务成功返回 `201` 和 HandoffTask；Chatwoot 标签/Assignment 在响应中以 `sync_status=pending|synced|failed` 表达。

`POST /conversations/{id}/resume-ai` 请求 `{version, reason}`。要求 Supervisor+；客诉、黑名单或拒绝联系存在时返回 `409 blocking_condition_exists`。成功后合并移除人工标签并重新计算状态。

## 6. Handoffs

| 方法与路径 | 请求/响应 | 权限 |
|---|---|---|
| `GET /handoffs` | status/priority/reason/team/assignee/q/page；返回 tab_counts | Agent+ |
| `GET /handoffs/{id}` | 任务、会话摘要、SLA、同步状态 | 数据范围内 |
| `POST /handoffs/{id}/claim` | `{version}` -> processing | Agent+ |
| `POST /handoffs/{id}/reassign` | `{platform_user_id?,chatwoot_agent_id?,team_id?,version}` | Supervisor+ |
| `POST /handoffs/{id}/complete` | `{resolution_note,version}` -> completed | 当前处理人/Supervisor |

领取时调用 Chatwoot Assignment。任务已领取返回 `409 handoff_already_claimed`，并在错误 `details` 中提供新的 assignee 摘要。Complete 不调用 Resume AI。

## 7. Leads 与媒体

| 方法与路径 | 行为 | 权限 |
|---|---|---|
| `GET /conversations/{id}/lead-captures` | 默认脱敏列表 | Agent+ |
| `POST /lead-captures/{id}/confirm` | 确认为有效联系方式 | Agent+ |
| `POST /lead-captures/{id}/invalidate` | 标记无效并记录原因 | Agent+ |
| `GET /lead-captures/{id}/reveal` | 返回一次完整值并审计 | 明确敏感字段权限 |
| `POST /media` | multipart 上传 SOP 媒体 | Operations Admin+ |
| `DELETE /media/{id}` | 仅未引用媒体可删除 | Operations Admin+ |

确认邮箱/电话后可调用 Chatwoot Contact Update；WeChat 写 Contact Custom Attribute。同步状态独立返回。

## 8. SOP

### 8.1 定义与草稿

- `GET /sops?status=&q=&page=`：列表和聚合。
- `POST /sops`：创建 definition 与 v1 草稿。
- `GET /sops/{id}`：定义、当前草稿、已发布版本摘要。
- `PUT /sops/{id}/draft`：全量保存草稿，要求 version。
- `POST /sops/{id}/copy`：复制为新草稿。
- `GET /sops/{id}/versions`：版本历史。

草稿请求：

```json
{
  "name": "报价后跟进",
  "description": "报价后未回复提醒",
  "inbox_ids": [128859],
  "audience": {"journey_stages": ["quoted"], "required_labels": [], "excluded_labels": ["人工接管", "客诉"]},
  "nodes": [
    {"key": "wait_1", "type": "wait_relative", "amount": 24, "unit": "hours", "basis": "last_outbound"},
    {"key": "send_1", "type": "send_message", "message": {"content_type": "text", "text": "想确认您是否需要调整行程。"}},
    {"key": "end", "type": "end"}
  ],
  "exit_rules": ["customer_replied", "human_handoff", "complaint", "lead_captured", "do_not_contact"],
  "rate_limits": {"per_conversation_per_day": 1, "per_contact_per_day": 1, "minimum_interval_minutes": 60},
  "version": 3
}
```

### 8.2 校验与生命周期

| 方法与路径 | 行为 |
|---|---|
| `POST /sops/{id}/validate` | 返回 errors、warnings、audience_estimate |
| `POST /sops/{id}/publish` | 生成不可变版本并进入 running |
| `POST /sops/{id}/pause` | 阻止未提交任务 |
| `POST /sops/{id}/resume` | 从已发布版本继续接受任务 |
| `POST /sops/{id}/archive` | 停止并只读归档 |

发布错误包括无 Inbox、过去时间、渠道不支持消息、无退出条件、频控缺失、媒体无效。24 小时窗口作为运行时检查，不因为当前受众都在窗口内就省略。

### 8.3 执行

- `GET /sops/{id}/executions?status=&page=`：Enrollment/Job/Outbound 联合视图。
- `GET /sops/{id}/metrics?from=&to=`：入组、计划、阻断、提交、送达、回复、留资。
- `POST /sop-jobs/{id}/retry`：仅确定可重试失败，Operations Admin+。
- `POST /sop-jobs/{id}/cancel`：未提交任务，Operations Admin+。

## 9. BI 与 Health

BI 查询公共参数：`from`、`to`、`inbox_id`、`channel`、`team_id`。无权限筛选值返回 `403`，不静默忽略。

| 路径 | 响应 |
|---|---|
| `GET /bi/overview` | 有效入站、AI 接管、人工、留资、异常及 definition |
| `GET /bi/trends` | 按日 AI/人工消息、首响、接管趋势 |
| `GET /bi/funnel` | 有效咨询到人工跟进漏斗 |
| `GET /bi/blockers` | 转人工、跳过、失败原因分布 |
| `GET /bi/sops` | SOP 触达、回复、留资和流失 |

Health：

- `GET /health`：公开，只返回 `ok/degraded`，不暴露配置。
- `GET /health/details`：已登录，按权限返回 DB、Worker、Chatwoot、AI、Webhook 和死信摘要。

## 10. Users、Roles 与 Audit

| 方法与路径 | 行为 | 权限 |
|---|---|---|
| `GET /users` | 用户、角色、Inbox 范围、Chatwoot 映射 | Super Admin |
| `POST /users` | 创建用户，不发送明文密码邮件；返回初始化方式 | Super Admin |
| `PATCH /users/{id}` | 状态、显示名、Chatwoot 映射、version | Super Admin |
| `PUT /users/{id}/roles` | 覆盖角色集合 | Super Admin |
| `PUT /users/{id}/inbox-scopes` | 覆盖授权范围 | Super Admin |
| `POST /users/{id}/revoke-sessions` | 撤销全部 Session | Super Admin |
| `GET /audit-logs` | 日期、actor、action、resource 分页 | Operations Admin+，范围受限 |

## 11. Chatwoot 对应接口清单

后端实现需覆盖：

| 本平台能力 | Chatwoot Application API |
|---|---|
| Inbox/Agent/Team/Label 同步 | `/inboxes`、`/agents`、`/inbox_members/{id}`、`/teams`、`/labels` |
| Webhook | `/webhooks` 与 `/webhooks/{id}` |
| 会话 | `/conversations`、`/conversations/{id}`、`/conversations/{id}/messages` |
| 标签 | `/conversations/{id}/labels`、`/contacts/{id}/labels` |
| 分配/状态 | `/conversations/{id}/assignments`、`/conversations/{id}/toggle_status` |
| 自定义字段 | `/conversations/{id}/custom_attributes`、Contact Update |
| 出站消息 | `/conversations/{id}/messages`，附件使用 multipart |
| 基础报表对账 | `/api/v2/accounts/{account_id}/reports` |

Chatwoot 请求失败不能直接把其 HTML/原始错误返回浏览器；转换为本契约稳定错误码，并在服务端以 request_id 关联脱敏日志。

