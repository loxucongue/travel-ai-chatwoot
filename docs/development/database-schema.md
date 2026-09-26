# SQLite 数据库设计

## 1. 通用约定

- 主键：本地表使用整数自增 `id`；外部 ID 使用 `chatwoot_*_id`。
- 时间：UTC ISO 8601，字段以 `_at` 结尾；租户另存 IANA 时区。
- 布尔：SQLite `INTEGER`，由 ORM 映射 Boolean。
- JSON：SQLAlchemy JSON；查询频繁字段必须拆列并建立索引。
- 乐观锁：可并发修改的表使用 `version INTEGER NOT NULL DEFAULT 1`。
- 软删除：配置类表使用 `deleted_at`；事件和审计不软删除。
- 敏感值：Token 加密；联系方式原值加密，另存脱敏值和哈希用于去重。

## 2. 租户、连接与权限

### `tenants`

| 字段 | 类型 | 约束/说明 |
|---|---|---|
| id | INTEGER | PK |
| name | TEXT | NOT NULL |
| timezone | TEXT | NOT NULL，默认 `Asia/Shanghai` |
| ai_enabled | BOOLEAN | NOT NULL |
| sop_enabled | BOOLEAN | NOT NULL |
| status | TEXT | `active/disabled` |
| created_at/updated_at | TEXT | NOT NULL |

### `chatwoot_connections`

| 字段 | 类型 | 约束/说明 |
|---|---|---|
| id/tenant_id | INTEGER | PK/FK，tenant 唯一启用连接 |
| base_url | TEXT | 仅允许 HTTPS |
| account_id | INTEGER | UNIQUE NOT NULL |
| encrypted_api_token | BLOB | NOT NULL |
| token_last4/token_version | TEXT/INTEGER | 不可用于鉴权 |
| connection_key | TEXT | UNIQUE，随机 32 bytes |
| encrypted_webhook_secret | BLOB | 可空 |
| webhook_id | INTEGER | 可空 |
| status/last_tested_at/last_error | TEXT | 连接健康 |
| created_at/updated_at | TEXT | NOT NULL |

### `inbox_bindings`

字段：`id`、`tenant_id`、`chatwoot_inbox_id`、`name`、`channel_type`、`ai_enabled`、`sop_enabled`、`default_team_id`、`business_hours_json`、`last_synced_at`、`status`、时间字段。唯一键 `(tenant_id, chatwoot_inbox_id)`。

### `users`、`roles`、`user_inbox_scopes`

- users：email 唯一、display_name、password_hash、status、last_login_at、created_at、updated_at。
- roles：固定 code、name；首期种子角色为五类 RBAC 角色。
- user_roles：`(user_id, role_id)` 联合主键。
- user_inbox_scopes：`user_id`、`inbox_binding_id`、可选 `chatwoot_team_id`、scope_type。
- `chatwoot_user_id` 可空，用于映射人工坐席，不作为本平台登录身份。

### `sessions`

字段：`id`、`session_hash UNIQUE`、`user_id`、`csrf_secret_hash`、`ip_hash`、`user_agent_hash`、`expires_at`、`last_seen_at`、`revoked_at`、`created_at`。过期和撤销 Session 每日清理。

## 3. Webhook 与任务

### `webhook_events`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER | PK |
| connection_id | INTEGER | FK |
| delivery_id | TEXT | 请求头可用时保存 |
| event | TEXT | Chatwoot 事件名 |
| account_id/inbox_id | INTEGER | 路由与查询 |
| resource_id | TEXT | message/conversation/contact ID |
| idempotency_key | TEXT | UNIQUE，`account:event:resource` |
| payload_json | JSON | 原始载荷，按保留策略清理 |
| status | TEXT | `pending/processing/completed/ignored/retry/dead` |
| attempts | INTEGER | 默认 0 |
| available_at | TEXT | 下次处理时间 |
| lease_owner/lease_expires_at | TEXT | Worker 租约 |
| ignore_reason/error_code/error_detail | TEXT | 可空，detail 脱敏 |
| received_at/processed_at | TEXT | 时间 |

索引：`(status, available_at)`、`(account_id, inbox_id, received_at)`、`delivery_id`。

资源 ID 不存在时，以规范化 Body SHA-256 作为幂等键尾部。`message_updated` 同一消息可能多次变化，幂等键追加状态或 payload 版本哈希，不能吞掉后续 delivered/failed 更新。

### `worker_heartbeats`

字段：`worker_id PK`、`worker_type`、`started_at`、`last_seen_at`、`metadata_json`。

## 4. 联系人、会话和消息

### `contacts`

字段：`id`、`tenant_id`、`chatwoot_contact_id`、`name`、`avatar_url`、`email_encrypted`、`email_masked`、`phone_encrypted`、`phone_masked`、`wechat_encrypted`、`wechat_masked`、`labels_json`、`custom_attributes_json`、`last_synced_at`、时间字段。唯一键 `(tenant_id, chatwoot_contact_id)`。

### `conversation_states`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER | PK |
| tenant_id/inbox_binding_id/contact_id | INTEGER | FK |
| chatwoot_conversation_id | INTEGER | 租户内唯一 |
| chatwoot_status | TEXT | open/pending/snoozed/resolved |
| can_reply | BOOLEAN | 最新镜像 |
| labels_json | JSON | Conversation 标签 |
| custom_attributes_json | JSON | Chatwoot 属性镜像 |
| assignee_id/team_id | INTEGER | Chatwoot ID，可空 |
| local_ai_override | TEXT | `inherit/enabled/disabled` |
| effective_ai_state | TEXT | 计算结果 |
| effective_state_reason | TEXT | 可解释原因 |
| journey_stage | TEXT | 本平台标准阶段 |
| last_customer_message_at | TEXT | SOP/窗口判断 |
| last_outbound_message_at | TEXT | SOP 判断 |
| processing_lease_owner/expires_at | TEXT | 会话串行锁 |
| version | INTEGER | 乐观锁 |
| last_synced_at/created_at/updated_at | TEXT | 时间 |

索引：`(inbox_binding_id, effective_ai_state, updated_at)`、`(contact_id, updated_at)`、`last_customer_message_at`。

### `message_events`

字段：`id`、`conversation_state_id`、`chatwoot_message_id`、`source_id`、`direction`、`private`、`content_type`、`content_text`、`status`、`sender_type`、`sender_id`、`attachments_json`、`external_error_json`、`chatwoot_created_at`、`received_at`。唯一键 `(conversation_state_id, chatwoot_message_id)`。

## 5. AI 与出站

### `ai_runs`

字段：`id`、`conversation_state_id`、`trigger_message_event_id UNIQUE`、`adapter_type`、`adapter_config_version`、`status`、`action`、`request_summary_json`、`response_summary_json`、`latency_ms`、`error_code`、`state_version_before`、`discard_reason`、`started_at`、`completed_at`。

原始请求响应默认不永久保存；只保存脱敏摘要。调试保留需单独配置并限制 7 天。

### `outbound_messages`

字段：`id`、`conversation_state_id`、`source_type`、`source_id`、`idempotency_key UNIQUE`、`content_type`、`content_summary`、`stored_media_id`、`chatwoot_message_id`、`status`、`submit_attempts`、`last_error_code`、`submitted_at`、`delivered_at`、`failed_at`、时间字段。

状态：`planned/blocked/submitting/submitted/delivered/read/failed/cancelled`。Create Message 200 只能进入 `submitted`。

## 6. 人工接管

### `handoff_tasks`

字段：`id`、`conversation_state_id`、`reason_code`、`reason_detail`、`priority`、`target_team_id`、`assignee_user_id`、`chatwoot_assignee_id`、`status`、`sync_status`、`sla_due_at`、`claimed_at`、`completed_at`、`completed_by`、`version`、时间字段。

同一会话最多一个 `pending/processing` 活跃任务，由应用事务保证并通过部分逻辑检查测试。状态：`pending/processing/completed/cancelled`。

## 7. SOP

### `sop_definitions`

字段：`id`、`tenant_id`、`name`、`description`、`status`、`current_draft_version_id`、`published_version_id`、`created_by`、时间字段、`deleted_at`。状态：`draft/running/paused/archived`。

### `sop_versions`

字段：`id`、`sop_definition_id`、`version_number`、`audience_json`、`nodes_json`、`exit_rules_json`、`rate_limits_json`、`channel_rules_json`、`validation_status`、`published_by`、`published_at`、`created_at`。唯一键 `(sop_definition_id, version_number)`；发布后不可修改。

### `sop_enrollments`

字段：`id`、`sop_version_id`、`conversation_state_id`、`status`、`current_node_key`、`entered_at`、`exited_at`、`exit_reason`、`last_customer_message_at_entry`。唯一键由策略的重复入组规则决定，MVP 同一版本同一会话只入组一次。

### `sop_jobs`

字段：`id`、`enrollment_id`、`node_key`、`job_type`、`scheduled_at`、`status`、`attempts`、`available_at`、`lease_owner`、`lease_expires_at`、`skip_reason`、`error_code`、`outbound_message_id`、时间字段。唯一键 `(enrollment_id, node_key)`。

索引：`(status, scheduled_at)`、`lease_expires_at`。

## 8. 留资、媒体、BI 与审计

### `lead_captures`

字段：`id`、`contact_id`、`conversation_state_id`、`type`、`value_encrypted`、`value_masked`、`value_hash`、`status`、`source`、`confidence`、`confirmed_by`、`confirmed_at`、时间字段。状态：`pending/confirmed/invalid`。只有 confirmed 计入留资率。

### `stored_media`

字段：`id`、`tenant_id`、`storage_path UNIQUE`、`original_name`、`mime_type`、`byte_size`、`sha256`、`status`、`created_by`、`created_at`、`deleted_at`。路径由后端生成，不接受用户提交的绝对路径。

### `daily_metrics`

字段：`id`、`tenant_id`、`metric_date`、`inbox_binding_id`、`team_id`、`metric_key`、`metric_value`、`dimensions_json`、`computed_at`。唯一键 `(tenant_id, metric_date, inbox_binding_id, team_id, metric_key)`。

### `audit_logs`

字段：`id`、`tenant_id`、`actor_user_id`、`action`、`resource_type`、`resource_id`、`before_json`、`after_json`、`request_id`、`ip_hash`、`created_at`。敏感值写入前脱敏；普通业务 API 不提供删除。

## 9. 保留与清理

| 数据 | 默认保留 |
|---|---|
| 原始 Webhook payload | 30 天 |
| 消息正文镜像 | 90 天，业务可调整 |
| AI 调试原文 | 默认关闭；开启时 7 天 |
| Session | 过期后 7 天清理 |
| Worker 死信 | 90 天 |
| 审计日志 | 至少 365 天 |
| 日指标 | 长期 |
| 媒体 | SOP 不再引用后按策略清理 |

清理任务必须先计算引用关系，不能删除仍被 SOP 版本或出站记录引用的媒体。

