# AI 客服运营平台：Chatwoot 集成开发矩阵

版本：v1.0  
核验日期：2026-08-16  
核验基线：Chatwoot Cloud Application API、Chatwoot Developer Docs、当前工作区内 Chatwoot `develop` 源码

## 1. 结论

核心方案可开发，MVP 不依赖 Chatwoot 付费 AI，也不依赖 Agent Bot：

```text
Facebook / Instagram 客户
  -> Chatwoot 渠道与会话
  -> Account Webhook
  -> 本平台事件入口
  -> AI 接管决策
  -> 客户 AI HTTP API
  -> Chatwoot Create Message
  -> 原渠道客户
```

Chatwoot 负责渠道、联系人、会话、消息、Inbox、团队、坐席和人工工作台。本平台负责 AI 控制、业务状态、SOP 调度、人工升级、BI、平台权限和审计。

## 2. 主要功能板块

| 编号 | 功能板块 | 核心能力 | 数据主系统 | MVP |
|---|---|---|---|---|
| 1 | 租户与 Chatwoot 接入 | 一个旅游公司绑定一个 Chatwoot Account、服务账号 Token 和多个 Inbox | 本平台配置 + Chatwoot | 必须 |
| 2 | Webhook 事件网关 | 验签、幂等、事件落库、快速响应、异步处理、死信重放 | 本平台 | 必须 |
| 3 | Inbox 接管配置 | 每个 Inbox 独立开启 AI、SOP、默认团队、时区和渠道限制 | 本平台 + Chatwoot Inbox | 必须 |
| 4 | 会话 AI 控制 | 全局、Inbox、会话、联系人四层规则；Chatwoot 标签实时同步 | 本平台状态 + Chatwoot 标签 | 必须 |
| 5 | AI 回复编排 | 拉取上下文、调用客户 AI、审核回复类型、发送、记录结果 | 本平台 + Chatwoot Messages | 必须 |
| 6 | 上下文与客户资料 | 历史消息、联系人资料、旅程阶段、已确认联系方式 | Chatwoot + 本平台 | 必须 |
| 7 | 人工接管 | 识别接管原因、暂停 AI/SOP、写标签、分配团队/坐席、通知员工 | 本平台 + Chatwoot Assignment | 必须 |
| 8 | SOP 策略中心 | 相对时间、固定时间、条件、节点、退出条件、频控、版本发布 | 本平台 | 必须 |
| 9 | SOP 执行与主动触达 | 调度、发送前复核、多媒体发送、失败重试、归因 | 本平台 + Chatwoot Messages | 必须 |
| 10 | 留资与客户旅程 | 邮箱、电话、WeChat 等识别、人工确认、阶段推进和标签同步 | 本平台 + Chatwoot Contact/Custom Attributes | 必须 |
| 11 | 运营总览与 BI | AI 接管率、转人工、卡点、SOP 触达、回复、留资、转化漏斗 | 本平台事件库，部分参考 Chatwoot Reports | 必须 |
| 12 | 平台用户与权限 | 运营管理员、主管、运营、BI，只允许查看授权 Inbox 数据 | 本平台 RBAC，参考 Chatwoot Inbox Members | 必须 |
| 13 | 可靠性与审计 | API 健康、Webhook 延迟、重试、熔断、配置审计、敏感数据访问 | 本平台 | 必须 |
| 14 | Chatwoot 资源管理 | 新增 Agent、Team、Inbox Members、Label 定义 | Chatwoot | 可选，优先在 Chatwoot 管理 |
| 15 | Agent Bot 模式 | outgoing_url、Bot 身份、Inbox 绑定 | Chatwoot Platform API | 后续，不阻塞 MVP |

## 3. AI 接管状态模型

### 3.1 不应只使用“客户标签”

同一个 Contact 可以在不同 Inbox 或不同时间存在多条 Conversation。只用 Contact Label 控制 AI 会误伤其他会话。

建议：

- **Conversation Labels**：控制当前会话是否由 AI 处理。
- **Contact Labels**：控制跨会话永久规则，例如拒绝联系、黑名单、敏感客户。
- **本平台配置**：控制租户和 Inbox 总开关。
- **本平台数据库**：保存计算后的最终状态、原因、版本和更新时间。

### 3.2 推荐标签

| 作用域 | 标签 | 含义 | 优先级 |
|---|---|---|---:|
| Contact | `拒绝联系` | 禁止 AI 和 SOP 主动发送 | 100 |
| Contact | `黑名单` | 禁止自动处理并提醒管理员 | 100 |
| Conversation | `客诉` | 强制人工，不能直接恢复 AI | 95 |
| Conversation | `人工接管` | 当前会话暂停 AI 和 SOP | 90 |
| Conversation | `AI关闭` | 人工显式关闭当前会话 AI | 85 |
| Conversation | `AI开启` | 人工显式开启当前会话 AI | 50 |
| Conversation | `已留资` | 停止获客型 SOP，可转销售跟进 | 40 |
| Conversation | `拒绝SOP` | 允许人工/AI 被动回复，但不主动跟进 | 80 |
| Conversation | `报价中` 等 | 业务阶段标签，不直接决定 AI | 10 |

长期建议把 `ai_state` 和 `journey_stage` 保存为 Conversation Custom Attributes，标签用于让 Chatwoot 人工可见和可操作。标签名称变更会影响规则，后台必须做固定映射而不是在代码里散落中文字符串。

### 3.3 最终状态优先级

```text
租户关闭
  > Inbox 关闭
  > Contact 强阻止标签
  > Conversation 强阻止标签
  > 渠道 can_reply=false / 时间窗口不允许
  > Conversation AI开启
  > Inbox 默认策略
```

状态建议：

- `AI_ACTIVE`
- `AI_PAUSED_GLOBAL`
- `AI_PAUSED_INBOX`
- `AI_PAUSED_LABEL`
- `HUMAN_HANDOFF`
- `CHANNEL_BLOCKED`
- `COMPLETED`

### 3.4 标签实时同步已确认

Chatwoot 源码中 Conversation 的允许更新字段包含 `label_list`。标签变更后会派发 `conversation_updated`：

```json
{
  "event": "conversation_updated",
  "id": 1028,
  "inbox_id": 128859,
  "labels": ["人工接管", "报价中"],
  "changed_attributes": [
    {
      "label_list": {
        "previous_value": ["AI开启", "报价中"],
        "current_value": ["人工接管", "报价中"]
      }
    }
  ]
}
```

Contact 标签变更会触发 `contact_updated`，当前标签主要从 `changed_attributes.label_list.current_value` 获取。

处理规则：

1. 订阅 `conversation_updated` 和 `contact_updated`。
2. 收到事件后更新本平台标签镜像并重新计算 `effective_ai_state`。
3. 标签事件只更新状态，不直接调用 AI。
4. 只有新的入站 `message_created` 才能触发被动 AI 回复。
5. 本平台写回标签后也会收到 Webhook，必须幂等，避免循环。
6. 每 5～15 分钟按活跃会话做增量对账，修复漏掉的 Webhook。

## 4. Webhook 事件清单

| 事件 | 必须 | 用途 | 关键过滤/字段 |
|---|---:|---|---|
| `message_created` | 是 | 新入站触发 AI；出站记录人工/AI/SOP 消息 | `id`, `message_type`, `private`, `content_type`, `attachments`, `conversation`, `inbox.id` |
| `message_updated` | 是 | 发送状态、失败、送达、已读变化 | `id`, `status`, `content_attributes.external_error` |
| `conversation_updated` | 是 | 标签、分配、团队、自定义字段和优先级变化 | `labels`, `custom_attributes`, `meta.assignee/team`, `changed_attributes` |
| `conversation_status_changed` | 是 | open/resolved/pending/snoozed 同步及任务取消 | `status`, `changed_attributes` |
| `contact_updated` | 是 | 联系方式、Contact 标签、自定义字段变化 | `email`, `phone_number`, `custom_attributes`, `changed_attributes` |
| `conversation_created` | 建议 | 建立本地映射和初始状态 | `id`, `inbox_id`, `meta.sender` |
| `contact_created` | 建议 | 建立 Contact 镜像 | `id`, `name`, `email`, `phone_number` |
| `webwidget_triggered` | 否 | Website Widget 特有，当前 Facebook MVP 不需要 | - |

### 4.1 入站消息过滤

满足全部条件才进入 AI 队列：

```text
event == message_created
message_type == incoming
private == false
account.id 已绑定
inbox.id 已启用
message.id 未处理
content_type 在支持范围
最终 AI 状态 == AI_ACTIVE
```

`external_echo` 只能作为辅助字段；主判断使用 `message_type=incoming`，防止平台自己的 outgoing 消息再次触发 AI。

### 4.2 安全与幂等

当前 Chatwoot 开源实现会发送：

- `X-Chatwoot-Delivery`：每次投递 UUID，可做事件幂等。
- `X-Chatwoot-Timestamp`：签名时间。
- `X-Chatwoot-Signature`：`sha256=HMAC_SHA256(secret, timestamp + '.' + raw_body)`。

Cloud 是否已经开放 Webhook Secret 需要通过一次真实投递确认。无论是否有签名，都需要保存 `message.id/event` 业务幂等键。

Webhook 接口应在校验和落库后快速返回 2xx，AI 调用通过异步队列执行，不能阻塞 Chatwoot Webhook。

## 5. Chatwoot API 完整清单

基础路径：`https://app.chatwoot.com/api/v1/accounts/{account_id}`  
鉴权头：`api_access_token: <server-side-token>`

### 5.1 连接与资源同步

| 方法 | 路径 | 用途 | MVP |
|---|---|---|---:|
| GET | `/inboxes` | 同步 Inbox、渠道、名称、channel_type | 必须 |
| GET | `/inboxes/{id}` | 获取单个 Inbox 和工作时间等配置 | 建议 |
| GET | `/agents` | 同步 Chatwoot 客服账号 | 必须 |
| GET | `/inbox_members/{inbox_id}` | 校验客服可访问的 Inbox | 必须 |
| GET | `/teams` | 同步团队 | 必须 |
| GET | `/teams/{team_id}/team_members` | 同步团队成员 | 建议 |
| GET | `/labels` | 同步标签定义 | 必须 |
| POST | `/labels` | 初始化本项目需要的标签定义 | 可选 |
| GET | `/custom_attribute_definitions` | 检查业务自定义字段是否存在 | 必须 |
| POST | `/custom_attribute_definitions` | 初始化 `journey_stage` 等字段 | 可选/管理员 |

不建议 MVP 在本平台修改 Agents、Teams 和 Inbox Members。先在 Chatwoot 管理，本平台只同步，减少权限和误操作风险。

### 5.2 Webhook 管理

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/webhooks` | 查询当前账号 Webhook |
| POST | `/webhooks` | 创建账号级事件订阅 |
| PATCH | `/webhooks/{webhook_id}` | 更新 URL、名称和订阅事件 |
| DELETE | `/webhooks/{webhook_id}` | 删除 Webhook |

核心订阅：

```json
[
  "message_created",
  "message_updated",
  "conversation_created",
  "conversation_updated",
  "conversation_status_changed",
  "contact_created",
  "contact_updated"
]
```

### 5.3 会话读取与控制

| 方法 | 路径 | 用途 | 关键字段/注意事项 |
|---|---|---|---|
| GET | `/conversations` | 列表、分页、搜索和筛选 | `status`, `assignee_type`, `inbox_id`, `team_id`, `labels`, `q`, `page` |
| GET | `/conversations/{conversation_id}` | 获取完整会话 | `can_reply`, `labels`, `custom_attributes`, `meta`, `status` |
| GET | `/conversations/{conversation_id}/messages` | 拉取历史消息 | `before` 每次最多 20，`after` 最多 100 |
| POST | `/conversations/{conversation_id}/assignments` | 转给 Agent 或 Team | `assignee_id`, `team_id` |
| POST | `/conversations/{conversation_id}/toggle_status` | 显式设置会话状态 | `open/resolved/pending/snoozed`；不要用它表示 AI 状态 |
| POST | `/conversations/{conversation_id}/custom_attributes` | 写旅程阶段等业务字段 | `custom_attributes` |
| GET | `/conversations/{conversation_id}/labels` | 获取当前完整标签列表 | 写标签前必须读取 |
| POST | `/conversations/{conversation_id}/labels` | 覆盖会话完整标签列表 | 不是增量增加；必须读取、合并、再提交 |

### 5.4 消息收发

| 方法 | 路径 | 用途 | 注意事项 |
|---|---|---|---|
| GET | `/conversations/{conversation_id}/messages` | AI 上下文、人工/AI/SOP 归因 | 分页读取，不能只依赖历史 Webhook |
| POST | `/conversations/{conversation_id}/messages` | AI 回复、SOP 触达、私有备注 | `message_type=outgoing`, `private`, `content_type`, `content_attributes`, `attachments[]` |
| POST | `/conversations/{conversation_id}/toggle_typing_status` | 可选显示正在输入 | `typing_status=on/off`，不是核心能力 |

文本示例：

```json
{
  "content": "您好，我可以继续为您介绍行程。",
  "message_type": "outgoing",
  "private": false,
  "content_type": "text"
}
```

Facebook 快捷回复示例：

```json
{
  "content": "您比较关心哪一项？",
  "message_type": "outgoing",
  "private": false,
  "content_type": "input_select",
  "content_attributes": {
    "items": [
      { "title": "行程日期" },
      { "title": "预算报价" },
      { "title": "转人工" }
    ]
  }
}
```

附件必须使用 `multipart/form-data` 的 `attachments[]` 上传文件。Chatwoot MessageBuilder 接收上传对象或内部 signed blob id，不应把临时图片 URL 直接当附件 JSON。SOP 媒体应保存在本平台稳定对象存储中，发送时下载、校验并上传给 Chatwoot。

### 5.5 联系人与留资

| 方法 | 路径 | 用途 | 注意事项 |
|---|---|---|---|
| GET | `/contacts/{contact_id}` | 获取联系人详情 | 邮箱、电话、自定义字段 |
| GET | `/contacts/search?q=...` | 按姓名、identifier、邮箱、电话搜索 | 用于运营搜索，不用于每条 Webhook 主链路 |
| PUT | `/contacts/{contact_id}` | 更新邮箱、电话、姓名、自定义字段 | 写入前校验人工确认状态 |
| GET | `/contacts/{contact_id}/labels` | 获取 Contact 标签 | 强阻止规则 |
| POST | `/contacts/{contact_id}/labels` | 覆盖 Contact 完整标签列表 | 同样需要读取、合并、再提交 |

推荐 Contact Custom Attributes：

- `wechat_id`
- `line_id`
- `lead_confirmed`
- `lead_confirmed_at`
- `lead_source`

推荐 Conversation Custom Attributes：

- `ai_state`
- `journey_stage`
- `handoff_reason`
- `last_sop_id`

### 5.6 报表

| 方法 | 路径 | 可提供指标 | 限制 |
|---|---|---|---|
| GET | `/api/v2/accounts/{account_id}/reports` | conversation、incoming/outgoing messages、first response、resolution | 无 AI 接管、SOP、留资和业务转化口径 |
| GET | `/api/v2/accounts/{account_id}/reports/conversations?type=account` | open、unattended、unassigned | 只作为补充健康指标 |

AI 接管率、接管原因、SOP 回复率和留资率必须来自本平台事件库。

## 6. SOP 策略设计

### 6.1 调度能力归属

Chatwoot 没有通用“未来某时发送消息”的 Application API。以下能力由本平台提供：

- 相对时间：事件发生后 `N` 分钟/小时/天。
- 固定时间：指定时区的某年某月某日某时。
- 业务时间：仅在工作日或营业时间发送。
- 多节点：发送后等待，再根据客户是否回复进入下一节点。
- 取消和跳过：客户回复、人工接管、留资、拒绝联系、状态关闭、渠道不可回复。
- 频控：单会话、单联系人、单 Inbox 和全租户限制。
- 重试：网络错误可退避重试；渠道策略拒绝不可盲目重试。

### 6.2 触发器

| 类型 | 示例 | 实现 |
|---|---|---|
| 相对客户入站 | 客户咨询后 30 分钟未回复完整资料 | 以 incoming message 时间创建任务 |
| 相对平台出站 | 发送方案 24 小时后仍无新入站 | 以成功发送 Message ID 和时间创建任务 |
| 相对阶段变化 | 进入“报价中”后 2 小时 | 监听标签或 Custom Attribute 变化 |
| 相对标签增加 | 增加“方案已发送”标签后 1 天 | `conversation_updated.changed_attributes.label_list` |
| 固定时间 | 2026-09-01 10:00 推送节庆提醒 | 保存租户时区和 UTC 执行时间 |
| 周期时间 | 每周一 10:00 检查符合条件客户 | 后续能力，必须有授权和频控 |
| 手工批次 | 运营筛选后提交一次触达 | 后续能力，需要审批和预估人数 |

### 6.3 节点类型

- 发送文本
- 发送图片
- 发送音频
- 发送视频
- 发送文档（仅支持渠道）
- Facebook 快捷回复
- 等待
- 条件分支
- 写入/移除标签
- 更新旅程阶段
- 转人工/分配团队
- 结束策略

### 6.4 Facebook 与 Instagram 可发送类型

| 类型 | Facebook | Instagram | Chatwoot 调用方式 |
|---|---|---|---|
| 文本 | 支持，最多 2,000 字符 | 支持，最多 1,000 字符 | `content_type=text` |
| 快捷回复 | 支持，当前源码用 `input_select` 转 quick replies | 当前发送服务未实现，按不支持处理 | `content_type=input_select` |
| 图片 | PNG/JPEG/GIF，最大 8MB | PNG/JPEG/GIF，最大 16MB | `attachments[]` |
| 音频 | AAC/M4A/WAV/MP4，最大 25MB | AAC/M4A/WAV/MP4，最大 25MB | `attachments[]` |
| 视频 | MP4/OGG/AVI/MOV/WEBM，最大 25MB | MP4/OGG/AVI/MOV/WEBM，最大 25MB | `attachments[]` |
| 文档 | PDF、Office 文件，最大 25MB | 不支持 | `attachments[]` |
| Cards/Form/Article | Chatwoot 数据结构存在，但当前 Messenger 发送服务不会可靠渲染为渠道原生卡片 | 不承诺 | MVP 禁用 |
| WhatsApp Template | 不适用 | 不适用 | 后续 WhatsApp 专用 `template_params` |

一条 Chatwoot Message 同时包含文本和多个附件时，Facebook/Instagram 发送服务会拆成文本和多个媒体发送动作，因此最终用户可能看到多条消息。SOP 编辑器需要提前预览实际拆分结果。

### 6.5 主动发送限制

- Facebook 和 Instagram 不能创建由企业发起的第一条会话；必须先有客户入站并形成 Conversation。
- 自动化促销消息原则上只能在客户最后一次入站后的 24 小时窗口内发送。
- Human Agent 最长窗口用于人工客服，不应被 AI/SOP 冒充使用。
- 发送前必须重新读取 Conversation，检查 `can_reply`、最新入站时间、人工标签、拒绝标签和 SOP 频控。
- `POST Create Message` 返回成功只代表 Chatwoot 创建了消息，仍需等待 `message_updated` 或读取消息 `status` 判断 delivered/failed。

## 7. 核心数据表建议

| 表 | 关键字段 |
|---|---|
| `tenants` | id, name, timezone, ai_enabled |
| `chatwoot_connections` | tenant_id, base_url, account_id, encrypted_token, webhook_id, webhook_secret_version |
| `inbox_bindings` | tenant_id, inbox_id, channel_type, ai_enabled, sop_enabled, default_team_id |
| `webhook_events` | delivery_id, event, account_id, inbox_id, resource_id, payload, received_at, processed_at, status |
| `conversation_states` | conversation_id, contact_id, inbox_id, labels, contact_labels, effective_ai_state, state_reason, version |
| `ai_runs` | message_id, conversation_id, request, response, status, latency_ms, error_code |
| `handoff_tasks` | conversation_id, reason, priority, team_id, assignee_id, status, sla_due_at |
| `sop_definitions` | tenant_id, name, status, current_version_id |
| `sop_versions` | trigger, audience, nodes, exit_rules, rate_limits, published_at |
| `sop_enrollments` | sop_version_id, conversation_id, entered_at, status, exit_reason |
| `sop_jobs` | enrollment_id, node_id, scheduled_at, locked_at, attempts, status, skip_reason |
| `outbound_messages` | conversation_id, source_type, source_id, chatwoot_message_id, status, sent_at |
| `lead_captures` | contact_id, conversation_id, type, masked_value, confirmed, confirmed_by, confirmed_at |
| `platform_users` / `roles` | tenant_id, user_id, role, inbox_scope |
| `audit_logs` | actor, action, resource, before, after, request_id, created_at |

## 8. 并发与一致性要求

1. 同一 Conversation 的 AI 决策和 SOP 发送必须使用分布式锁或数据库行锁。
2. 发送前使用最新 Conversation/Contact 状态，不能只相信任务创建时快照。
3. 人工加上 `人工接管` 标签后，所有未发送 AI/SOP 任务立即取消。
4. AI 生成期间收到人工接管事件时，生成结果必须丢弃，不能继续发送。
5. 写标签使用“读取、合并、条件更新”策略；Chatwoot 标签 API 会覆盖列表。
6. Webhook 至少一次投递，消费者必须幂等。
7. 每日或每小时做活跃会话对账，修复 Webhook 丢失和人为配置漂移。

## 9. MVP 开发前最小实验

| 实验 | 单一变量 | 成功信号 | 失败后的处理 |
|---|---|---|---|
| 会话标签实时同步 | 在 Chatwoot 手工增加/移除 `AI关闭` | 收到 `conversation_updated`，包含新 labels/label_list，平台状态在 3 秒内变化 | 改为 Webhook + 30 秒轮询对账 |
| Contact 标签同步 | 手工增加 `拒绝联系` | 收到 `contact_updated.changed_attributes.label_list` | 收事件后调用 Contact Labels GET 获取完整列表 |
| 文本 SOP 发送 | 活跃测试会话发送文本 | Message API 200，最终 status=delivered | 保存错误码，区分权限/窗口/内容错误 |
| 图片 SOP 发送 | 上传一张合规 JPEG | Multipart 创建成功且 Messenger 收到图片 | 检查 MIME、大小、重定向和对象存储下载 |
| 快捷回复 | Facebook 发送 `input_select` | Messenger 显示可点击选项且点击产生 incoming | 退化为普通文本编号菜单 |
| 24 小时窗口 | 改变最后入站时间 | 24 小时内发送成功，窗口外被平台阻止 | 不允许 SOP 进入发送 API |
| 人工竞态 | AI 生成中添加 `人工接管` | AI 结果被取消且无消息发出 | 增加发送前二次状态校验和锁 |
| Webhook 验签 | 检查 Cloud 请求头 | 有 Delivery、Timestamp、Signature 且验签通过 | 使用随机路径 Secret + IP/速率限制，并保留业务幂等 |
| 最小权限 Token | 服务账号仅加入 Inbox 128859 | 可读会话、发消息、标签、分配，但不能访问未授权 Inbox | 管理动作与日常发送拆分两个 Token |

## 10. 开发判断

### 已确认可实现

- 新消息自动触发客户 AI HTTP API 并回复。
- Chatwoot 手工修改会话标签后实时暂停/恢复 AI。
- Contact 强阻止标签跨会话生效。
- 查询完整历史聊天，不需要只依赖历史 Webhook。
- 写入标签、自定义字段、联系人联系方式。
- 转人工并分配团队/坐席。
- 相对时间和固定时间 SOP。
- Facebook 文本、图片、音频、视频、文档和快捷回复。
- Instagram 文本、图片、音频和视频。
- AI 接管、卡点、SOP、留资和转人工 BI。

### 不能由 Chatwoot 单独提供

- AI 开关和多级优先级。
- SOP 调度器和节点执行引擎。
- AI/SOP 业务统计和归因。
- 旅游业务阶段和留资确认。
- 本平台 RBAC 和敏感数据脱敏。
- Webhook 重放、死信、AI 熔断和跨系统审计。

### 当前不能承诺

- Facebook/Instagram 在无客户首条消息时主动开聊。
- AI/SOP 使用 Human Agent 7 天窗口发送自动化促销内容。
- Instagram 文档发送。
- Facebook/Instagram 原生 Cards/Form/Article 在当前 Chatwoot Application API 下稳定展示。
- 仅凭 Create Message HTTP 200 就认为渠道送达成功。

## 11. 官方依据

- Webhooks: https://developers.chatwoot.com/api-reference/webhooks/add-a-webhook
- Conversations List: https://developers.chatwoot.com/api-reference/conversations/conversations-list
- Conversation Details: https://developers.chatwoot.com/api-reference/conversations/conversation-details
- Get Messages: https://developers.chatwoot.com/api-reference/messages/get-messages
- Create Message: https://developers.chatwoot.com/api-reference/messages/create-new-message
- Conversation Assignment: https://developers.chatwoot.com/api-reference/conversation-assignments/assign-conversation
- Conversation Labels: https://developers.chatwoot.com/api-reference/conversations/add-labels
- Contact Labels: https://developers.chatwoot.com/api-reference/contact-labels/add-labels
- Conversation Custom Attributes: https://developers.chatwoot.com/api-reference/conversations/update-custom-attributes
- Contact Update: https://developers.chatwoot.com/api-reference/contacts/update-contact
- Inboxes: https://developers.chatwoot.com/api-reference/inboxes/list-all-inboxes
- Inbox Members: https://developers.chatwoot.com/api-reference/inboxes/list-agents-in-inbox
- Agents: https://developers.chatwoot.com/api-reference/agents/list-agents-in-account
- Teams: https://developers.chatwoot.com/api-reference/teams/list-all-teams
- Labels: https://developers.chatwoot.com/api-reference/labels/list-all-labels
- Custom Attribute Definitions: https://developers.chatwoot.com/api-reference/custom-attributes/add-a-new-custom-attribute
- Reports: https://developers.chatwoot.com/api-reference/reports/get-account-reports
- Channel capability matrix: https://developers.chatwoot.com/self-hosted/supported-features
- Conversation label update source: https://github.com/chatwoot/chatwoot/blob/develop/app/models/conversation.rb
