# Stitch 页面功能与字段审查

## 审查基线

- 产品需求：[PRD-ai-customer-service-platform-v1.md](./PRD-ai-customer-service-platform-v1.md)
- 实际 Chatwoot 工作区：`account_id=180474`
- 实际 Facebook Inbox：`inbox_id=128859`
- 已验证 Webhook：`message_created`
- 已验证 API：历史消息、发送消息、标签、分配、状态、联系人、团队和坐席

## 总体结论

Stitch 的五个页面可以作为视觉和信息架构基准，但其中部分字段来自通用 SaaS/销售场景，当前没有 Chatwoot 或自建事件库的数据来源。高保真实现保留深色固定侧栏、浅灰工作区、Geist 字体、紧凑表格和状态色体系；业务内容以 PRD 为准。

## 页面审查

| 页面 | Stitch 内容 | 审查结论 | 高保真实现 |
|---|---|---|---|
| 运营总览 | Teams、Best Performing Employees | 当前不是核心目标，且缺少统一绩效口径 | 替换为有效入站、AI 接管、人工队列、留资、异常和渠道健康 |
| 运营总览 | Emails Exchanged | 当前仅验证 Facebook Inbox | 替换为 AI 回复、人工回复、SOP 触达和留资漏斗 |
| 会话控制台 | Company、Plan、LTV Estimate | Webhook 无此字段，业务系统也未接入 | 移除；展示渠道、会话 ID、阶段、标签、团队、联系方式和 AI 状态 |
| 会话控制台 | AI Copilot | 容易与 Chatwoot Captain 混淆 | 改为自建平台 `ai_state` 开关，并显示最终生效原因 |
| 会话控制台 | 输入框直接回复 | PRD 规定正式人工回复仍在 Chatwoot | 页面只提供运营备注；客户回复入口为“在 Chatwoot 中打开” |
| 人工队列 | Accept | 可由 Chatwoot Assignment API 实现 | 保留“领取”，并展示分配结果、等待时间和接管原因 |
| 人工队列 | 恢复 AI | 可实现，但必须检查客诉/拒绝联系等阻止条件 | 保留，放入会话处理动作，不允许直接跳过确认 |
| SOP | 新建、编辑、激活 | 可由自建 SOP 服务实现 | 保留，使用结构化进入条件、退出条件、频控和节点 |
| SOP | 邮件节点 | 当前没有已验证 Email Inbox | 不作为默认示例；默认使用 Facebook 文本节点 |
| 系统设置 | Website、Email、API 多渠道已连接 | 与实际资产不符 | 只将 Facebook 标为已连接；Instagram 标为待接入 |
| 系统设置 | API Token 明文输入 | 存在泄漏风险 | 仅显示服务端已托管状态，不回显 Token |
| 系统设置 | 测试连接即测试发送 | 可能向真实客户发送消息 | 默认只读连接测试；发送测试需明确选择测试会话并二次确认 |

## 可用字段

### Chatwoot Webhook/API 可直接获得

- `account.id`
- `inbox.id`
- `conversation.id`
- `sender.id`
- `conversation.channel`
- `message.id/source_id`
- `message_type`
- `content/content_type`
- `private`
- `content_attributes.external_echo`
- `conversation.can_reply`
- `conversation.status`
- `conversation.labels`
- `conversation.meta.assignee/team`
- 联系人邮箱、电话及自定义属性

### 自建平台提供

- `ai_state`
- `journey_stage`
- `handoff_reason/priority`
- `lead_captured_at`
- SOP 定义、实例、节点和执行结果
- AI Run、跳过原因、失败原因和耗时
- 用户角色、收件箱数据范围和审计日志

### 当前不可假设存在

- 客户公司、套餐、LTV、订单金额和支付状态
- 员工评分和“最佳员工”排名
- 跨渠道统一客户 ID
- Instagram/WhatsApp/Email 已连接状态
- 客户是否已读渠道消息的统一可靠字段

## 组件约束

- 侧栏固定宽度，桌面端 248px，平板折叠为图标栏，移动端使用抽屉。
- 页面区块不使用装饰性卡片嵌套；卡片只用于指标、表格和工具面板。
- 普通卡片圆角不超过 8px。
- 命令按钮使用图标加文本；纯工具按钮使用 Lucide 图标并带 tooltip/aria-label。
- 状态徽标使用语义色：绿色 AI、琥珀人工、红色客诉/异常、蓝色留资/信息。
- 所有开关同时显示名称与最终状态，不能只靠颜色表达。
- 表格提供空态、加载态、错误态和无权限态的视觉规范。
- Token、联系方式和其他敏感数据默认脱敏。

## 2026-08-16 逐页能力复核

状态说明：

- **原生**：Chatwoot Application API 有正式接口或响应字段。
- **自建**：技术上可实现，但数据和执行逻辑必须由 AI 平台后端提供。
- **组合**：需要先更新本平台状态，再调用一个或多个 Chatwoot API。
- **条件**：受 Chatwoot 版本、套餐、Platform API 权限或渠道规则限制。
- **原型**：当前只有前端交互，不应宣称已经操作真实数据。

### 全局导航与命令栏

| 文案/按钮 | 状态 | 实现与边界 |
|---|---|---|
| 工作区 `china2go` | 自建 | 对应本平台租户；同时保存 `chatwoot_account_id=180474`。Chatwoot 本身没有本平台租户字段。 |
| 全局搜索 | 组合 | 会话可使用 Conversations List 的 `q`；联系人和功能导航由本平台分别查询和路由。 |
| 通知 | 自建 | 接管、异常和 SOP 失败通知来自本平台事件表；Chatwoot 没有统一返回这些业务通知。 |
| AI 服务可用/重试中 | 自建 | 来自 AI HTTP 健康检查、队列和失败重试记录。 |
| 当前用户和角色 | 组合 | Chatwoot Agents 返回用户及 `agent/administrator/custom_role_id`；本平台再映射自己的 RBAC。 |
| 深色模式“后续” | 原型 | 已明确标记后续，不构成当前可用功能。 |

### 运营总览

| 文案/按钮/字段 | 状态 | 实现与边界 |
|---|---|---|
| 渠道筛选 | 原生 | 通过 Inboxes List 获取渠道，并用 Conversations List 的 `inbox_id` 筛选。未授权渠道必须禁用。 |
| 今天/7天/30天 | 组合 | Chatwoot Reports 接受 `since/until`；AI、SOP、留资指标需查询本平台事件库。 |
| 有效入站会话 | 组合 | 由 `message_created + message_type=incoming + private=false` 去重；“有效”规则属于本平台。 |
| AI 接管率 | 自建 | Chatwoot 没有 `ai_state`；以 AI Run 成功或本平台会话状态为准。 |
| 待人工接管/超过 SLA | 自建 | Chatwoot 可提供未分配会话，但接管原因和内部 SLA 是本平台字段。Chatwoot SLA Policy 为 Enterprise 条件能力。 |
| 成功留资率 | 组合 | 邮箱/电话来自 Contact；WeChat 和“已确认”来自 Contact Custom Attributes；分母口径由本平台定义。 |
| 系统异常 | 自建 | AI 超时、重试、队列失败和 Webhook 处理失败来自本平台日志。 |
| 消息处理趋势 | 组合 | Chatwoot Reports 支持 incoming/outgoing 数量；区分 AI 与人工需结合 AI Message/Run 记录或 sender。 |
| 客户旅程漏斗 | 自建 | `journey_stage` 建议写入 Conversation Custom Attributes；漏斗快照和去重由本平台计算。 |
| 自动回复耗时 | 自建 | 从入站 Webhook 接收时间到 Chatwoot Create Message 成功时间计算。 |
| 连接健康 | 自建 | Webhook 成功率、AI 接口和重试数来自本平台监控；只能把 Chatwoot API 探活标为 Chatwoot 健康。 |
| 查看会话/人工队列 | 原生前端 | 本地路由跳转，可直接实现。 |

### 会话控制台

| 文案/按钮/字段 | 状态 | 实现与边界 |
|---|---|---|
| 搜索联系人、会话 ID 或消息 | 组合 | Conversations List 的 `q` 支持消息内容；联系人姓名/ID 精确搜索可能需要 Contacts API 后再关联。 |
| AI 状态筛选 | 自建 | `AI 接管/人工接管/暂停` 均为本平台状态，不是 Chatwoot conversation.status。 |
| 消息历史 | 原生 | `GET /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages`，支持 `before/after` 分页。 |
| AI 开关 | 自建 | 更新本平台 `ai_state`；可同步标签，但不能把 Chatwoot `status` 当 AI 开关。 |
| 打开 Chatwoot | 条件 | URL 可按账号和会话 ID 生成，但原型 ID 不是真实会话，所以当前保持禁用。接入真实 ID 后启用。 |
| 转人工 | 组合 | 写本平台接管记录和 `ai_state`，读取并合并标签，再调用 Conversation Assignment API。 |
| 运营备注 | 组合 | 当前定义为本平台内部备注；若希望 Chatwoot 同步，可调用 Create Message 且 `private=true`。 |
| 渠道可回复 | 原生 | 使用 `conversation.can_reply`，发送前必须再次校验。 |
| 分配团队/坐席 | 原生 | 会话响应包含 assignee/team；修改使用 Assign Conversation。 |
| 当前阶段 | 自建在 Chatwoot | 先定义 Conversation Custom Attribute `journey_stage`，再调用 Update Custom Attributes。 |
| 标签 | 原生 | List Labels 可读；Add Labels 实际会覆盖完整列表，因此必须先读、合并、再 POST。 |
| 邮箱/电话 | 原生 | Contact 字段，可能为空。修改使用 Update Contact。 |
| WeChat | 自建在 Chatwoot | Contact Custom Attribute，Chatwoot 没有原生 `wechat` 字段。 |
| Contact ID/Conversation ID | 原生 | Webhook 和 Conversations API 均可获得。 |

### 人工接管队列

| 文案/按钮/字段 | 状态 | 实现与边界 |
|---|---|---|
| 待领取/处理中/已完成 | 自建 | Chatwoot 只有会话状态和分配关系；接管任务生命周期由本平台记录。 |
| 原因、P1/P2/P3、等待时间 | 自建 | 原因和优先级来自规则结果；等待时间以接管任务创建时间计算。可选同步 Chatwoot priority。 |
| 团队/坐席 | 原生 | Teams、Agents、Assignments API。 |
| 领取 | 组合 | 校验当前用户属于 Inbox，再把会话分配给当前 Chatwoot User，并更新本平台任务。 |
| 完成 | 自建 | 完成接管任务不等于 Chatwoot resolved；是否关闭会话必须作为独立动作。AI 默认继续暂停。 |
| 刷新和筛选 | 组合 | 本平台接管任务查询，必要时关联 Chatwoot 会话当前状态。 |
| 内部 SLA | 自建 | 当前页面使用本平台 SLA，不依赖 Enterprise `sla_policy_id`。 |

### SOP 配置

| 文案/按钮/字段 | 状态 | 实现与边界 |
|---|---|---|
| 运行中 SOP、成功触达、回复率、阻止数 | 自建 | 全部来自本平台 SOP 定义、实例和节点执行表。Chatwoot Reports 不提供 SOP 指标。 |
| 新建/编辑/复制/暂停 | 自建 | 本平台 CRUD；节点编辑器不属于 Chatwoot。当前前端为原型交互。 |
| 目标人群 | 组合 | 通过 Conversation List 的 inbox/team/labels/status 加本平台 `journey_stage/ai_state` 查询。 |
| 定时触达 | 组合 | 本平台队列调度，到期后再次校验，最后调用 Chatwoot Create Message。 |
| 回复率 | 自建 | SOP 成功发送后，在归因窗口内收到新的 incoming message 才计回复。 |
| 留资率 | 自建 | 联系方式确认事件进入归因窗口才计入。不能仅因文本疑似包含号码就计入。 |
| 安全退出条件 | 组合 | 客户回复、人工接管、拒绝联系、`can_reply=false`、渠道窗口限制和频控均需发送前检查。 |
| Instagram SOP 草稿 | 条件 | 可以保留草稿，但在 Instagram Inbox 授权完成前不能发布或执行。 |

### 系统设置：连接、Webhook 与 Bot

| 文案/按钮/字段 | 状态 | 实现与边界 |
|---|---|---|
| Base URL/Account ID | 原生配置 | 平台租户连接配置。Account ID 不是客服 User ID。 |
| API Token | 原生鉴权 | 使用专用服务账号 Token，必须只存后端密钥库，绝不能下发浏览器。权限受该用户角色限制。 |
| 只读测试 | 原生 | 调用 Inboxes、Agents、Conversations GET 接口，不发送消息。 |
| 同步 Chatwoot | 原生 | Inboxes、Inbox Members、Agents、Teams、Labels 可读取。 |
| 全局/Inbox/Conversation AI 开关 | 自建 | 均为本平台字段，按租户 > Inbox > 会话优先级求最终状态。 |
| Account Webhook | 原生 | `POST /api/v1/accounts/{account_id}/webhooks`，属于账号，不属于客服，也不能原生限定单个 Inbox。 |
| Webhook 事件 | 原生 | 可订阅 message/conversation/contact 等官方事件；AI 主链路最低需要 `message_created`。 |
| Webhook Inbox 范围 | 自建过滤 | 后端收到账号级事件后按 `inbox.id` 过滤。切勿为每个客服创建重复 Webhook。 |
| Agent Bot outgoing_url | 条件 | 创建 Bot 使用 Platform API；是否能在 Chatwoot Cloud 使用取决于账户能力和凭证。 |
| Agent Bot 绑定 Inbox | 原生/条件 | 已有 Agent Bot 后可调用 `POST .../inboxes/{id}/set_agent_bot`；不是 MVP 必需。 |
| AI HTTP 地址、超时、重试、熔断 | 自建 | 是本平台调用客户 AI 的配置，不是 Chatwoot 配置。 |

### 系统设置：用户与权限

| 文案/按钮/字段 | 状态 | 实现与边界 |
|---|---|---|
| Chatwoot 客服账号 | 原生 | Agents API 可列出/新增，基础角色为 `agent/administrator`。 |
| Inbox 成员 | 原生 | Inbox Members 可列出、增加、更新和移除客服。 |
| Team 成员 | 原生 | Team Members API 可管理。 |
| 自定义角色 | 条件 | `custom_role_id` 字段存在，但自定义角色能力取决于 Enterprise。 |
| 本平台角色 | 自建 | 运营管理员、客服主管、客服/销售、BI 只读由本平台 RBAC 表管理。 |
| 数据范围 | 自建 + 原生校验 | 每次请求先校验本平台 scope，再校验 Inbox/Team 成员关系；不能只隐藏前端按钮。 |
| 联系方式脱敏 | 自建 | 根据权限在后端序列化时脱敏，浏览器不能先拿明文再用 CSS 隐藏。 |
| 新增客服 | 组合 | 可调用 Chatwoot Agents API，然后建立本平台用户映射和 Inbox Members。 |
| 启用/停用 | 自建 | 本平台登录状态与 Chatwoot 用户状态是两个对象，必须分别处理。 |

## 官方接口依据

- Account Webhooks: https://developers.chatwoot.com/api-reference/webhooks/add-a-webhook
- List Webhooks: https://developers.chatwoot.com/api-reference/webhooks/list-all-webhooks
- Conversations List: https://developers.chatwoot.com/api-reference/conversations/conversations-list
- Conversation Messages: https://developers.chatwoot.com/api-reference/messages/get-messages
- Create Message / Private Note: https://developers.chatwoot.com/api-reference/messages/create-new-message
- Conversation Assignment: https://developers.chatwoot.com/api-reference/conversation-assignments/assign-conversation
- Conversation Labels: https://developers.chatwoot.com/api-reference/conversations/add-labels
- Conversation Custom Attributes: https://developers.chatwoot.com/api-reference/conversations/update-custom-attributes
- Contacts: https://developers.chatwoot.com/api-reference/contacts/update-contact
- Agents: https://developers.chatwoot.com/api-reference/agents/list-agents-in-account
- Inbox Members: https://developers.chatwoot.com/api-reference/inboxes/list-agents-in-inbox
- Teams: https://developers.chatwoot.com/api-reference/teams/list-all-teams
- Inboxes: https://developers.chatwoot.com/api-reference/inboxes/list-all-inboxes
- Reports: https://developers.chatwoot.com/api-reference/reports/get-account-reports
- Agent Bots: https://developers.chatwoot.com/api-reference/agentbots/create-an-agent-bot
- Bind Agent Bot to Inbox: https://developers.chatwoot.com/api-reference/inboxes/add-or-remove-agent-bot

## 需要最小实验确认的共同未知

1. **Chatwoot Cloud 是否允许当前账户创建 Agent Bot**：变量只切换为 Cloud 当前套餐与凭证；成功信号是可获得 Agent Bot ID 并成功 `set_agent_bot`，失败则继续使用 Account Webhook，不影响 MVP。
2. **Facebook 主动消息窗口**：使用已同意测试的真实会话，在不同最后入站时间下各发送一次；记录 `can_reply`、HTTP 状态和 Meta/Chatwoot 返回，确定 SOP 实际可发送窗口。
3. **服务账号最小权限**：新建专用 Agent 并只加入 Inbox 128859；依次测试读会话、发消息、标签、分配和 Webhook 管理，缺少的管理员动作拆到单独管理凭证，不扩大日常发送 Token 权限。
