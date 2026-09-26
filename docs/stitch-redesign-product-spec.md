# China2Go AI 客服运营平台：Stitch 前端重设计说明书

> 文档用途：作为 Stitch 生成新一版高保真前端信息架构、页面和交互的唯一产品输入。
>
> 基准日期：2026-08-17。
>
> 基准范围：以当前仓库已经实现的 React/FastAPI/SQLite 功能和真实 Chatwoot Cloud 联调结果为准。本文不把规划能力伪装成现有能力。

## 1. 产品背景

### 1.1 产品目标

这是一个建立在 Chatwoot 之上的 AI 客服运营控制平台，面向旅游公司的 Facebook、Instagram、WhatsApp、LINE 等公域客户承接场景。

现阶段已经真实接入：

- Chatwoot Cloud Account：`180474`
- Facebook Inbox：`128859`
- Facebook Page：`CITS 國旅環球 - China2Go`
- 本地平台通过 Cloudflare Relay 接收 Chatwoot Account Webhook
- 本地 Worker 调用 Mock AI 或企业 HTTP AI，再通过 Chatwoot API 回复

业务目标不是替代 Chatwoot，而是补充 Chatwoot 原生工作台之外的运营控制能力：

1. 决定某个 Inbox、联系人或会话是否允许 AI 接管。
2. 接收客户消息并调用企业 AI 自动回复。
3. 在客诉、留资、AI 失败等场景切换人工。
4. 配置受安全门控制的主动 SOP 触达。
5. 统计 AI、人工、留资、成交和 SOP 效果。
6. 管理平台账号、Chatwoot Agent 绑定和 Inbox 数据范围。

### 1.2 Chatwoot 与本平台的职责边界

| 能力 | Chatwoot | 本平台 |
|---|---|---|
| Facebook/Instagram 等渠道授权 | 负责 | 不重复实现 OAuth |
| 渠道消息收发 | 负责 | 通过 Chatwoot API 间接发送 |
| 人工客服输入框与完整客服工作台 | 负责 | 不在一期重复实现人工聊天输入框 |
| 联系人、会话、Agent、Team、Label 主数据 | 主数据源 | 建立本地镜像并提供运营控制 |
| AI 是否接管 | 不负责本项目规则 | 负责最终状态计算和发送前复核 |
| 企业 AI 调用 | 不负责 | Mock/HTTP Adapter |
| 人工接管任务与 SLA | 基础分配 | 负责任务、领取、通知、显式恢复 AI |
| SOP 主动触达 | 非本项目依赖 | 负责调度、安全门和执行明细 |
| 业务 BI | 基础报表 | 负责 AI、人工、留资、成交、SOP 业务指标 |

重要设计结论：会话页的“打开 Chatwoot”是明确工作流，不要在本平台伪造一个尚未实现的完整人工回复编辑器。

### 1.3 当前运行环境与边界

- 前端：React 19、TypeScript、Vite。
- 后端：FastAPI、SQLAlchemy、SQLite。
- 进程：单 API + 单 Worker。
- 环境：本地开发和小规模真实联调，不是多实例生产架构。
- 公网事件入口：Cloudflare Worker/Relay，而不是依赖本机 Tunnel 常驻连接。
- 当前 Chatwoot 显示全部会话 `66` 个，本地镜像 `14` 个，因为全量历史回填尚未启动。
- 历史回填启动后可以拉取全部会话；历史数据不会触发 AI、SOP、通知或人工接管。
- 当前主动发送默认关闭，不能在视觉稿中表现为“创建即自动实发”。

## 2. 用户角色与权限

### 2.1 角色定义

| 角色 | 核心职责 | 页面范围 |
|---|---|---|
| Admin | 系统配置、用户、全部 Inbox、SOP 发布、BI、审计 | 全部页面 |
| Supervisor | 指定 Inbox 的会话、人工队列、SOP、BI、改派 | 总览、会话、人工接管、SOP |
| Agent | 指定 Inbox 的会话、标签、领取和完成自己的人工任务 | 会话、人工接管 |

### 2.2 数据范围

- Admin 默认可访问全部 Inbox。
- Supervisor 和 Agent 通过 `inbox_binding_ids` 限制可见数据。
- 每个 Agent 可绑定一个 `chatwoot_agent_id`。
- Agent 未被分配某会话时，邮箱、电话等敏感联系方式必须脱敏。
- Agent 被分配会话后可以查看完整联系方式。
- Supervisor 和 Admin 可以直接查看其数据范围内的完整联系方式。

### 2.3 当前权限实现注意事项

- 当前前端导航已经按角色隐藏页面。
- 设置页只向 Admin 开放。
- 当前 Supervisor 的改派下拉无法读取平台用户列表，因为 `/v1/users` 仅允许 Admin；重设计可以保留改派入口，但开发时需要补一个按 Inbox 返回可分配客服的接口。
- 当前会话分配接口仍需进一步收紧 Agent 权限；设计上 Agent 只能分配给自己，Supervisor/Admin 才能分配给其他客服。
- 不要仅依赖按钮隐藏表达权限，所有无权限状态都需要后端 `403` 反馈界面。

## 3. 全局信息架构

### 3.1 认证前页面

1. 登录。
2. 首次登录强制修改密码。

### 3.2 主导航

1. 运营总览。
2. 会话控制台。
3. 人工接管。
4. SOP 配置。
5. 系统设置。

### 3.3 全局框架

| 区域 | 内容 | 交互 |
|---|---|---|
| 左侧导航 | 品牌、Workspace、主导航、系统状态 | 移动端折叠抽屉 |
| 顶部栏 | 页面上下文、全局搜索入口、通知、帮助、用户菜单 | 全局搜索目前只跳转会话页 |
| 通知中心 | 未读数、标题、正文、关联会话 | 10 秒轮询；点击标记已读并跳转会话 |
| 用户菜单 | 姓名、角色、退出 | 当前仅支持退出 |
| Toast | 保存、同步、领取等操作反馈 | 自动消失，不替代字段级错误 |

### 3.4 全局状态规范

所有页面必须设计以下状态：

- 首次加载 Skeleton 或明确 Loading。
- 无数据 Empty State。
- API 失败 Error State，包含重试操作。
- 无权限 `403`。
- Session 过期 `401`，返回登录页。
- 写操作进行中，禁用重复提交。
- 写操作成功 Toast。
- 并发冲突 `409`，提示数据已被其他用户更新并刷新。
- 危险操作二次确认。

## 4. 页面一：登录

### 4.1 页面目的

使用本平台账号进入 AI 运营平台。这里不是 Chatwoot 登录，也不使用 Facebook 登录。

### 4.2 可用字段

| 字段 | 类型 | 规则 |
|---|---|---|
| `email` | Email | 必填，平台用户邮箱 |
| `password` | Password | 必填，8 至 200 字符 |

### 4.3 操作与状态

- “登录”调用 `POST /v1/auth/login`。
- 登录成功后获取 Session Cookie 和 CSRF Token。
- 错误包括账号不存在、密码错误、账号停用和后端不可达。
- 不提供“Facebook 登录”“Chatwoot 登录”或自行注册。
- 当前没有找回密码邮件流程；密码重置由 Admin 完成。

## 5. 页面二：首次登录强制修改密码

### 5.1 页面目的

Admin 创建或重置用户后，只展示一次临时密码。用户首次登录必须修改密码才能进入主系统。

### 5.2 字段

| 字段 | 类型 | 规则 |
|---|---|---|
| 当前临时密码 | Password | 必填 |
| 新密码 | Password | 至少 10 位 |
| 确认新密码 | Password | 必须与新密码一致 |

### 5.3 操作

- “修改密码并进入”调用 `POST /v1/auth/change-password`。
- “退出登录”撤销当前 Session。
- 修改前不能进入其他页面。

## 6. 页面三：运营总览

### 6.1 页面目的

向运营主管展示指定时间范围内 AI、人工、留资和成交的真实业务指标。数据来自本地事件镜像，不展示虚构指标。

### 6.2 使用角色

Admin、Supervisor。Agent 不显示该页面。

### 6.3 当前筛选器

- 今天：`days=1`。
- 7 天：`days=7`。
- 30 天：`days=30`。

当前 API 的总览支持可选 `inbox_id`，但现有 UI 尚未提供 Inbox 筛选。渠道、客服、SOP 综合筛选尚未完整实现，不要在 Stitch 稿中把它们标记为已可用；可以放入“需要 API 扩展”的设计区。

### 6.4 指标字段

| 字段 | 中文含义 | 统计口径 |
|---|---|---|
| `conversations` | 有效会话 | 时间范围内更新的会话数 |
| `incoming_messages` | 入站消息 | 非私密客户消息 |
| `outgoing_messages` | 出站消息 | 非私密出站消息 |
| `ai_only` | AI 独立处理 | 有 AI 回复且没有人工出站的会话 |
| `mixed` | AI + 人工 | 同时存在 AI 和人工出站的会话 |
| `human_only` | 纯人工 | 有人工出站且没有 AI 回复的会话 |
| `ai_handled` | AI 已参与 | 至少关联一个本平台 AI Run 的会话 |
| `handoffs` | 人工接管总数 | 时间范围内创建的接管任务 |
| `handoff_pending` | 待接管 | 状态为 pending |
| `handoff_overdue` | 超过 SLA | pending/claimed 且已超过截止时间 |
| `leads` | 已留资 | 上线后新增留资标签的会话 |
| `conversions` | 已成交 | 上线后新增成交标签的会话 |
| `ai_errors` | AI 异常 | 状态为 failed 的 AI Run |
| `inferred_history` | 历史人工推断 | 接入前无法精确识别发送者的 outgoing |

### 6.5 页面模块

1. 五个 KPI：有效会话、AI 参与率、待人工接管、上线后留资率、AI 异常。
2. 消息处理趋势：按日展示 AI 回复、人工回复、客户消息。
3. 客户旅程漏斗：新增会话、AI 已参与、已留资、已成交。
4. 处理结构：AI 独立、AI+人工、纯人工。
5. 下钻入口：查看会话、查看人工接管队列。

### 6.6 边界

- AI 消息只有关联本平台 `ai_run/outbound_message` 才算 AI。
- 接入前未知 outgoing 显示为推断人工，不能冒充精确客服归因。
- 留资和成交只统计 `analytics_cutover_at` 之后的标签新增事件。
- 当前漏斗是会话计数，不是唯一联系人计数，也不是订单金额。
- 当前没有收入、报价金额、客单价字段。

## 7. 页面四：会话控制台

### 7.1 页面目的

查看本地镜像的真实 Chatwoot 会话、消息、AI 最终状态、标签和客服分配，并执行 AI 控制相关操作。

本页面不是完整人工聊天工作台。人工回复应点击“打开 Chatwoot”。

### 7.2 使用角色

Admin、Supervisor、Agent，但只能读取其 Inbox 范围。

### 7.3 推荐布局

桌面端采用三栏：

1. 左栏：会话搜索、总数、分页或无限加载、会话列表。
2. 中栏：会话头部、消息流、回到最新消息。
3. 右栏：AI 状态、客服、标签、联系方式、数据来源。

移动端不要强行保持三栏。建议使用“会话列表 → 会话详情”两级导航，右侧上下文放入详情抽屉或标签页。

### 7.4 会话列表字段

| 字段 | 类型 | 展示含义 |
|---|---|---|
| `id` | Number | Chatwoot Conversation ID |
| `name` | String | 客户联系人名称，不是客服名称 |
| `channel` | String | 例如 `Channel::FacebookPage` |
| `inbox` | String | Inbox 名称 |
| `inbox_id` | Number | Chatwoot Inbox ID |
| `last_message` | String | 最近一条公开 incoming/outgoing；排除系统 activity |
| `ai_state` | Enum | AI 最终状态 |
| `ai_reason` | String | 状态原因代码 |
| `labels` | String[] | Chatwoot 会话标签 |
| `can_reply` | Boolean | Chatwoot 当前是否允许回复 |
| `status` | String | Chatwoot 会话状态 |
| `version` | Number | 本地并发控制版本 |
| `updated_at` | DateTime | 本地最近更新时间 |
| `chatwoot_url` | URL | 打开 Chatwoot 会话 |

### 7.5 会话分页边界

- API 已支持 `page`、`page_size`、`q`。
- 当前前端默认只读取第一页 25 条，尚未实现加载更多。
- Chatwoot 当前真实总数为 66，本地历史回填前只有 14。
- 新设计必须包含分页、无限滚动或“加载更多”，不能只展示总数而无法浏览剩余记录。

### 7.6 AI 状态枚举

| 状态 | 中文 | 含义 |
|---|---|---|
| `AI_ACTIVE` | AI 接管 | 满足所有允许条件 |
| `HUMAN_HANDOFF` | 人工接管 | 存在待处理/处理中人工任务 |
| `AI_PAUSED_LABEL` | 标签关闭 | 会话或联系人阻断标签命中 |
| `AI_PAUSED_INBOX` | Inbox 关闭 | Inbox AI 开关关闭 |
| `AI_PAUSED_GLOBAL` | 全局关闭 | AI Adapter 或租户级开关关闭 |
| `CHANNEL_BLOCKED` | 渠道阻断 | `can_reply=false` 或渠道窗口限制 |

设计时状态与原因必须同时展示。不要仅用一个绿色/红色圆点表达复杂状态。

### 7.7 消息字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Number | Chatwoot Message ID |
| `direction` | Enum | `incoming/outgoing/activity` |
| `private` | Boolean | 私密备注，不应被当作客户消息 |
| `content_type` | String | text/image/video/audio/file 等 |
| `content` | String | 正文或附件占位符 |
| `status` | String | submitted/sent/delivered/read/failed 等 |
| `attribution` | Enum | customer/ai/sop/human/inferred_human/system |
| `attachments` | Object[] | Chatwoot 附件元数据 |
| `created_at` | DateTime | 消息时间 |

### 7.8 消息视觉规则

- `incoming + customer`：客户消息。
- `outgoing + ai`：AI 回复，必须有明确 AI 标识。
- `outgoing + sop`：SOP 主动消息，不能和 AI 回复混为一类。
- `outgoing + human/inferred_human`：人工消息；推断人工需要弱提示。
- `private=true`：私密备注样式，不能表现为已发送给客户。
- `activity/system`：居中的系统事件，不使用客户或客服气泡。
- 附件必须根据类型显示预览或文件信息；当前前端只显示文本占位，属于待完善视觉能力。

### 7.9 会话详情字段

| 字段 | 类型 | 规则 |
|---|---|---|
| 客户名称 | String | 来自 Chatwoot Contact |
| 邮箱 | String/Null | 按权限脱敏 |
| 电话 | String/Null | 按权限脱敏 |
| `pii_masked` | Boolean | 是否已脱敏 |
| 当前 Agent | Object/Null | id、name、availability_status、thumbnail |
| 当前 Team | Object/Null | id、name |
| 当前标签 | String[] | Chatwoot 最新标签 |
| 可选 Agent | Object[] | 当前 Inbox 成员 |
| 可选标签 | Object[] | id、title、description、color |

### 7.10 页面操作

| 操作 | 结果 | 限制 |
|---|---|---|
| 搜索 | 按客户名、会话 ID、消息摘要过滤 | 服务端查询，本地镜像范围 |
| 转人工 | 创建人工任务并立即停止本地 AI | 默认 P2、原因 manual |
| 分配客服 | 调 Chatwoot Assignment API | Agent 仅应分配自己；需后端补强 |
| 同时停止 AI | 分配前增加人工接管标签 | 默认开启 |
| 编辑标签 | 与 Chatwoot 当前标签做差量合并 | 避免覆盖并发新增标签 |
| 创建标签 | 创建 Chatwoot 账号标签 | Admin |
| 打开 Chatwoot | 新标签页打开人工工作台 | 所有可访问用户 |
| 回到最新 | 消息流滚动到底部 | 仅离开底部时出现 |

### 7.11 实时与同步

- 会话列表和消息当前每 10 秒刷新。
- 标签编辑会立即写回 Chatwoot；Chatwoot 手工修改标签通过 Webhook 回流。
- 新会话收到首个 Webhook 后建立本地镜像。
- 历史会话只有执行全量回填后才完整出现。
- 设计可以表达“最近同步时间”，但不要宣称 WebSocket 毫秒级实时。

## 8. 页面五：人工接管队列

### 8.1 页面目的

把需要真人处理的会话转化为有 SLA、可领取、可改派和可完成的任务。人工任务与 Chatwoot 会话分配同步。

### 8.2 任务来源

- Chatwoot 新增人工接管或客诉标签。
- AI Adapter 返回 `handoff`。
- 平台用户手工发起。
- AI 连续失败达到阈值。

### 8.3 状态

| 状态 | 含义 |
|---|---|
| `pending` | 待领取 |
| `claimed` | 已领取/处理中 |
| `completed` | 处理完成，但 AI 仍未恢复 |
| `cancelled` | 已取消；当前 UI 未提供专门页签 |

### 8.4 列表字段

| 字段 | 说明 |
|---|---|
| `id` | 本平台 Handoff Task ID |
| `conversation_id` | Chatwoot Conversation ID |
| `customer_name` | 客户名称 |
| `inbox` / `inbox_id` | Inbox 信息 |
| `reason_code` | manual/label/ai_handoff/ai_error 等 |
| `reason_detail` | 详细原因 |
| `priority` | P1/P2/P3 |
| `status` | 任务状态 |
| `assignee` | 平台客服姓名 |
| `assignee_user_id` | 平台用户 ID |
| `sla_due_at` | SLA 截止时间 |
| `created_at` | 创建时间 |
| `claimed_at` | 领取时间 |
| `completed_at` | 完成时间 |
| `version` | 乐观锁版本 |
| `labels` | 当前会话标签 |

### 8.5 页面操作

- 页签：待接管、处理中、已完成，显示各状态数量。
- 搜索：客户、会话和原因。
- 领取：把任务分配给当前平台用户绑定的 Chatwoot Agent。
- 改派：Supervisor/Admin 选择其他已绑定 Chatwoot Agent 的用户。
- 完成：只改变任务状态，不自动恢复 AI。
- 恢复 AI：Supervisor/Admin 显式移除阻断标签，并重新读取 Chatwoot 状态。
- 刷新：立即重新拉取；默认 10 秒轮询。

### 8.6 关键边界

- 同一任务多人同时领取时，只有一个 `version` 能成功，其他人收到 `409`。
- 用户没有绑定 Chatwoot Agent 时不能领取。
- Agent 只能完成自己的任务。
- “完成”与“恢复 AI”必须是两个明确动作，不能合并成一个按钮。
- 默认 SLA 30 分钟，目前页面没有按 Inbox 编辑 SLA 的设置入口。

## 9. 页面六：SOP 配置管理

### 9.1 页面目的

配置有限多节点的主动触达流程，支持演练、受控实发和逐任务执行记录。它不是无限循环营销自动化平台。

### 9.2 使用角色

Admin、Supervisor。Agent 不显示。

### 9.3 列表与汇总字段

| 字段 | 说明 |
|---|---|
| `id` | SOP ID |
| `name` | 名称 |
| `description` | 说明 |
| `status` | draft/running/paused/archived |
| `version` | 版本号 |
| `dry_run` | 演练模式 |
| `live_enabled` | 是否允许白名单实发 |
| `trigger_type` | manual/label/stage |
| `trigger_labels` | 触发标签 |
| `inbox_ids` | 限定 Chatwoot Inbox ID |
| `nodes` | 节点列表，最多 20 个 |
| `exit_labels` | 退出标签 |
| `stop_on_incoming` | 客户回复后退出 |
| `frequency_hours` | 联系人频控小时数，1 至 720 |
| `enrolled` | 入组数 |
| `sent` | submitted/delivered 数 |
| `blocked` | skipped 数 |
| `updated_at` | 更新时间 |

页面顶部汇总：运行中、真实提交、已入组、安全阻止。

### 9.4 SOP 编辑字段

| 字段 | 类型 | 规则 |
|---|---|---|
| 名称 | String | 1 至 200 字符 |
| 说明 | Text | 最多 2000 字符 |
| 触发方式 | Enum | manual/label/stage |
| 触发标签 | String[] | 标签/阶段触发时使用 |
| Inbox | Number[] | 当前 UI 尚未提供选择，payload 暂传空数组 |
| 退出标签 | String[] | 建议默认人工接管、客诉、拒绝联系、已留资、已成交 |
| 客户回复退出 | Boolean | 当前固定 true，建议新设计显式展示 |
| 频控 | Number | 默认 24 小时 |
| 演练模式 | Boolean | 新建默认 true |
| 允许白名单实发 | Boolean | 新建默认 false |

### 9.5 节点字段

| 字段 | 类型 | 规则 |
|---|---|---|
| `key` | String | 节点唯一键 |
| `schedule_type` | Enum | relative/fixed |
| `delay_minutes` | Number | 相对延时，0 至 525600 |
| `fixed_at` | DateTime | 固定时间 |
| `basis` | Enum | enrollment/last_customer_reply/previous_node |
| `content_type` | Enum | text/image/video/audio/file |
| `content` | Text | 最多 10000 字符，可作为附件说明 |
| `media_id` | Number/Null | 本地上传附件 ID |

### 9.6 安全发送门

真实发送必须同时满足：

1. SOP 状态为 running。
2. `dry_run=false`。
3. `live_enabled=true`。
4. 会话命中配置的 SOP 白名单标签。
5. `can_reply=true`。
6. 会话 AI 状态仍允许自动化。
7. 没有人工接管。
8. 没有退出标签。
9. 客户没有在入组后回复。
10. 没有命中频控。

不满足时必须显示 `skip_reason`，不能只显示“失败”。常见原因：

- `dry_run`
- `blocked_not_whitelisted`
- `channel_cannot_reply`
- `ai_or_handoff_blocked`
- `exit_label`
- `customer_replied`
- `frequency_limited`
- `sop_not_running`
- `node_missing`

### 9.7 页面操作

- 搜索、状态筛选。
- 新建、编辑、复制。
- 校验、发布、暂停。
- 手工选择会话入组。
- 标签或阶段触发自动入组。
- 上传图片、视频、音频和文件，单文件当前限制 20MB。
- 查看执行明细。

当前前端尚未展示 `/sops/{id}/executions` 的执行明细页面。新设计应增加一个详情页或右侧抽屉，字段包括：`conversation_id`、`node_key`、`scheduled_at`、`status`、`skip_reason`、`attempts`、`outbound_message_id`。

### 9.8 渠道边界

- 当前真实联调渠道为 Facebook。
- 发送必须经过 Chatwoot 的 `can_reply` 判断，不绕过 Meta 消息窗口。
- 一期只做文本和普通附件。
- 不支持按钮、快捷回复、WhatsApp Template 或渠道专用交互消息。

## 10. 页面七：系统设置

系统设置只对 Admin 开放。建议保留左侧二级导航或顶部可滚动 Tabs，但移动端必须可清楚定位当前子页。

### 10.1 子页：渠道与收件箱

目的：配置一套 Chatwoot 连接，同步资源，并控制各 Inbox 的 AI 开关。

连接字段：

| 字段 | 说明 |
|---|---|
| `base_url` | 例如 `https://app.chatwoot.com` |
| `account_id` | Chatwoot Account ID |
| `api_token` | 只写，前端不能读取明文 |
| `token_last4` | 仅用于提示已配置 |
| `status` | unconfigured/configured/connected/error |
| `last_tested_at` | 最近连接测试 |
| `last_error` | 最近错误码 |

操作：保存、只读测试、同步资源。

同步资源返回：Inbox 数、Agent 数、Team 数、Label 数、同步时间。

Inbox 字段：本地 `id`、`chatwoot_inbox_id`、名称、渠道类型、AI 开关、状态、最近同步时间。

Inbox AI 开关只影响本平台自动回复，不影响 Chatwoot 人工收发。

### 10.2 子页：Account Webhook

目的：在 Chatwoot 账号级配置事件回调。一个 Chatwoot Account 配置一个 Webhook，不是每个客服账号配置一个 Webhook。

字段：

- Webhook ID。
- 回调 URL。
- 订阅事件。
- 最近接收时间。

必选事件：`message_created`、`message_updated`、`conversation_updated`、`conversation_status_changed`、`contact_updated`。

可选事件：`conversation_created`、`contact_created`。

不要设计成用户输入任意 Webhook URL；平台生成固定 URL，再保存到 Chatwoot。

### 10.3 子页：历史数据

目的：从 Chatwoot 分页回填全部会话和消息。

字段：

| 字段 | 说明 |
|---|---|
| `status` | not_started/pending/running/paused/completed/failed |
| `phase` | idle/initial/verification/done 等 |
| `current_page` | 当前页 |
| `total_items` | Chatwoot 总会话数 |
| `completed_items` | 当前扫描阶段完成数 |
| `failed_items` | 失败数 |
| `error_code` | 最近失败原因 |
| `updated_at` | 最近更新 |
| `completed_at` | 完成时间 |

操作：启动、暂停、继续、失败重试。

边界：历史消息只入库；不会触发 AI、SOP、通知和人工任务。第一遍完成后再验证扫描一次。

### 10.4 子页：AI Adapter

目的：选择测试 Mock 或企业 HTTP AI。

公共字段：

- `adapter`：mock/http。
- `enabled`：是否启用。
- `timeout_seconds`：2 至 120。
- `failure_handoff_threshold`：1 至 20。

Mock 字段：`trigger_text`、`reply_text`。

HTTP 字段：URL、Bearer Token。Token 只写；响应只返回 `token_configured`。

HTTP AI 输入包括会话、联系人、当前消息、历史消息、标签和 Inbox。

HTTP AI 输出协议：

```json
{
  "action": "reply|handoff|no_action",
  "reply": "可选文本",
  "handoff_reason": "可选原因",
  "stage": "可选阶段",
  "captured_contacts": []
}
```

企业 AI 的旅游业务提示词、方案生成和报价逻辑不属于本平台通用设置页。

### 10.5 子页：标签映射

目的：把 Chatwoot 标签映射为本平台业务状态。

| 字段 | 默认值 | 用途 |
|---|---|---|
| `handoff_labels` | 人工接管、客诉 | 创建人工任务并停止 AI |
| `contact_block_labels` | 拒绝联系、黑名单 | 跨会话阻断自动化 |
| `lead_labels` | 已留资 | 留资统计 |
| `conversion_labels` | 已成交 | 成交统计 |
| `stage_labels` | 空 | 旅程阶段与 SOP 触发 |
| `sop_whitelist_label` | SOP测试白名单 | 主动实发安全门 |

Chatwoot 修改会话标签后通过 Webhook 更新本地状态。联系人标签用于跨会话阻断，不要与会话标签的人工接管混为一谈。

### 10.6 子页：用户与权限

目的：创建平台用户、绑定 Chatwoot Agent 并限制 Inbox 范围。

用户字段：

- `id`、邮箱、显示名称。
- 角色：admin/supervisor/agent。
- `active`。
- `must_change_password`。
- `chatwoot_agent_id`。
- `inbox_binding_ids`。
- `created_at`。

创建操作返回一次性 `temporary_password`。关闭弹窗后无法再次读取，只能重置。

后端已经支持但当前前端尚未完整呈现的操作：编辑姓名、角色、Agent 绑定、Inbox 范围；重置密码。新设计应提供用户详情抽屉或编辑页。

停用用户不能继续登录。不要设计删除用户，以保留审计关联。

### 10.7 子页：通知 Webhook

目的：把人工接管事件发送到企业内部通知系统，同时保留站内通知。

字段：启用开关、Webhook URL、HMAC Secret、事件类型。

当前事件：`handoff.created`、`handoff.overdue`。

Secret 只写；响应只返回 `secret_configured`。请求使用 HMAC SHA-256 签名，失败重试最多 5 次后进入 dead 状态。

当前没有“测试发送”接口，视觉稿不要放一个假按钮；若需要该按钮必须同时列为后端新增需求。

### 10.8 子页：审计日志

目的：查看关键管理操作，不能编辑。

字段：`id`、`user_id`、`action`、`resource_type`、`resource_id`、`details`、`created_at`。

当前前端展示时间、操作、资源和用户 ID；新设计可以增加动作类型筛选、用户筛选、时间筛选和详情抽屉，但筛选需要扩展 API。

## 11. 全局通知中心

### 11.1 字段

| 字段 | 说明 |
|---|---|
| `id` | Notification ID |
| `event_type` | handoff.created/handoff.overdue |
| `title` | 标题 |
| `body` | 正文 |
| `conversation_id` | 可选关联会话 |
| `read_at` | 已读时间 |
| `created_at` | 创建时间 |

### 11.2 交互

- 顶栏显示未读点或数量。
- 10 秒轮询。
- 点击通知标记已读。
- 有 `conversation_id` 时跳转对应会话。
- 当前会话页尚未完整处理 URL 中的 `conversation` 参数，新设计可以包含深链行为，但开发时需要补齐。

## 12. 关键业务流程

### 12.1 自动回复

```text
客户发消息
→ Chatwoot 收到
→ Account Webhook 写入本地
→ Worker 判断 AI 最终状态
→ Mock/HTTP AI 返回决策
→ 发送前重新读取 Chatwoot 标签与 can_reply
→ Chatwoot Create Message
→ 状态从 submitted 更新为 delivered/failed
```

### 12.2 人工接管

```text
标签/AI/人工/失败阈值触发
→ 创建 Handoff Task
→ AI 立即停止
→ 站内和外部通知
→ 客服领取
→ Chatwoot 分配到绑定 Agent
→ 客服在 Chatwoot 处理
→ 平台标记完成
→ Supervisor/Admin 显式恢复 AI
```

### 12.3 SOP

```text
SOP 草稿
→ 校验
→ 发布
→ 手工或标签入组
→ 生成节点任务
→ 到期重新检查所有安全门
→ 演练记录 / 安全跳过 / Chatwoot 实发
→ 记录执行状态和原因
```

### 12.4 历史回填

```text
Admin 启动
→ 分页读取全部会话
→ 逐会话分页读取消息
→ 本地幂等更新
→ 第二遍验证扫描
→ 完成
```

## 13. 不可伪造的功能与数据

以下能力当前不存在或不完整，Stitch 不应把它们设计成无需开发即可使用：

- 本平台内完整人工消息编辑器。
- 电话、语音通话和视频客服。
- 订单、报价金额、支付、合同和收入字段。
- AI 知识库、提示词编辑器和旅游线路生成逻辑。
- WhatsApp Template、按钮、轮播和渠道专用交互消息。
- 无限循环或复杂分支 SOP。
- 多租户计费、套餐、账单和商户自助 OAuth。
- 毫秒级 WebSocket 实时更新。
- 通知 Webhook 测试发送。
- 完整全局搜索结果页。
- 已实现的 66 条会话前端连续分页；当前需要补开发。
- 精确识别接入前每一条 outgoing 的具体客服。

## 14. Stitch 视觉与交互要求

### 14.1 产品气质

- 这是高频运营工具，不是营销官网。
- 信息密度应高但层级清晰。
- 优先扫描、比较和重复操作效率。
- 避免超大 Hero、装饰性渐变、漂浮卡片和大面积单色主题。
- 卡片圆角不超过 8px。
- 页面区块尽量使用自然分栏和边界，不要层层卡片嵌套。

### 14.2 控件规范

- 工具操作使用 Lucide 图标并提供 Tooltip。
- 二元设置使用 Toggle/Checkbox。
- 模式切换使用 Segmented Control。
- 选项集使用 Select/Menu。
- 数值使用 Number Input。
- 危险实发使用明确确认，不使用普通绿色按钮淡化风险。
- 状态不能只靠颜色，必须同时有文本和图标。

### 14.3 响应式要求

- 桌面目标宽度：1440px。
- 移动目标宽度：390px。
- 会话桌面三栏，移动端两级导航。
- 表格移动端转换为行卡片或明确横向滚动，不能裁掉操作按钮。
- SOP 长表单使用固定 Header/Footer 和可滚动 Body。
- 左侧会话列表与中间消息流必须独立滚动。

## 15. Stitch 需要输出的页面与状态

### 15.1 必须输出的主页面

1. 登录。
2. 首次修改密码。
3. 运营总览。
4. 会话控制台。
5. 人工接管队列。
6. SOP 列表。
7. SOP 新建/编辑。
8. SOP 执行明细。
9. 系统设置：渠道与收件箱。
10. 系统设置：Account Webhook。
11. 系统设置：历史数据。
12. 系统设置：AI Adapter。
13. 系统设置：标签映射。
14. 系统设置：用户与权限。
15. 系统设置：通知 Webhook。
16. 系统设置：审计日志。

### 15.2 必须输出的组件或交互状态

- 全局通知下拉。
- 用户菜单。
- 会话分配客服弹窗。
- 会话标签选择和新建标签弹窗。
- 人工任务改派弹窗。
- SOP 手工入组选择器。
- 加载、空数据、错误、无权限、Session 过期。
- `409` 并发冲突。
- AI 状态变化提示。
- SOP 演练、禁止发送、白名单实发三种安全状态。
- 历史同步未开始、运行中、暂停、失败、完成。
- 桌面和移动版本。

## 16. API 对照表

| 页面 | 主要接口 |
|---|---|
| 登录/改密 | `/v1/auth/login`、`/logout`、`/me`、`/csrf`、`/change-password` |
| 总览 | `/v1/bi/overview`、`/trends`、`/funnel`、`/handoff-reasons`、`/sop-performance` |
| 会话 | `/v1/conversations`、`/{id}`、`/{id}/messages`、`/{id}/controls`、`/{id}/assignment`、`/{id}/labels`、`/labels` |
| 人工接管 | `/v1/handoffs`、`/{id}/claim`、`/assign`、`/complete`、`/restore-ai` |
| SOP | `/v1/sops`、`/{id}`、`/validate`、`/publish`、`/pause`、`/enroll`、`/executions`、`/v1/media` |
| Chatwoot 设置 | `/v1/settings/chatwoot`、`/test`、`/sync`、`/webhook`、`/inboxes`、`/resources` |
| AI/标签/通知 | `/v1/settings/ai`、`/ai-adapter`、`/label-mappings`、`/notifications` |
| 历史数据 | `/v1/settings/history-sync`、`/status`、`/pause` |
| 用户权限 | `/v1/users`、`/{id}`、`/reset-password`、`/v1/permissions` |
| 通知/审计 | `/v1/notifications`、`/{id}/read`、`/v1/audit-logs` |

## 17. 设计验收标准

Stitch 输出应满足：

1. 每个显示字段都能在本文找到来源或明确标记需要新增 API。
2. 不把客服昵称显示为客户名称。
3. 私密消息、系统事件、AI、SOP、人工消息视觉可区分。
4. 会话总数超过单页时能够浏览后续页面。
5. 人工任务完成后不会误导用户认为 AI 已自动恢复。
6. SOP 默认演练，实发风险在列表、编辑和发布确认中都清晰可见。
7. Token、Secret 和临时密码不会作为普通可回显字段长期展示。
8. Agent 的敏感信息脱敏和操作权限有明确界面反馈。
9. 所有主页面具备桌面和移动方案。
10. 所有按钮都有真实接口或明确的“需要后端新增”标记。
