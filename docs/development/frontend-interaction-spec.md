# 前端页面与交互规范

## 1. 全局原则

现有 `frontend/` 的视觉样式和五页导航继续使用，数据源从 `src/data.ts` 静态数组切换为 API。新增登录路由，Hash 页面可暂时保留，但认证状态和 API Client 必须统一管理。

### 1.1 API Client

- Base URL：`https://api.luoxuecong.asia/v1`，从 Vite 环境变量读取。
- 所有请求设置 `credentials: include`。
- 写请求携带 `X-CSRF-Token`。
- `401`：清除当前用户状态并跳转登录，保留原目标页。
- `403`：显示无权限页，不把它当普通空数据。
- `409`：显示冲突详情并重新拉取受影响资源。
- `422`：字段旁显示服务端校验信息。
- `429/5xx`：保留当前输入，显示可重试错误。

列表统一使用服务端游标或页码，不在浏览器对全量数据筛选。搜索输入 300ms 防抖，切换筛选立即取消上一请求。

### 1.2 通用状态

每个页面必须实现：

- 初次加载骨架屏，不显示伪造统计数字。
- 空数据说明和清除筛选入口。
- 加载失败、重试和请求 ID。
- 保存中禁用重复提交。
- 成功 Toast 只在服务器确认后显示。
- 乐观更新仅用于低风险开关，并在失败时回滚。
- 页面离开前提示未保存表单。
- 无 Inbox 权限与无数据使用不同状态。

### 1.3 实时刷新

MVP 不增加 WebSocket。会话列表、人工队列和系统健康在页面可见时每 10 秒轮询，SOP 执行明细每 15 秒轮询。用户正在编辑的表单不被轮询覆盖；若服务器版本变化，显示“数据已更新，请重新加载”。

## 2. 登录与会话

### 2.1 登录页

字段：邮箱、密码。操作：

| 操作 | API | 成功 | 失败 |
|---|---|---|---|
| 打开页面 | `GET /auth/me` | 已登录则进入原目标页 | 未登录继续显示表单 |
| 登录 | `POST /auth/login` | 获取 CSRF，进入总览 | 字段错误/账号停用/凭据错误 |
| 退出 | `POST /auth/logout` | 清空前端状态并回登录 | 本地仍清空，记录失败 Toast |

连续错误不提示邮箱是否存在。提交期间按钮显示“登录中”。没有管理员时页面显示“系统尚未初始化，请在本机执行管理员 CLI”，不开放注册按钮。

### 2.2 个人菜单

- 显示当前用户、角色和可访问 Inbox 数量。
- “退出登录”调用 API。
- 原型中的下拉箭头必须打开菜单，不允许无行为。
- 深色模式仍不在 MVP，移除导航项而不是展示不可用按钮。

## 3. 全局导航和搜索

导航按权限显示：BI Viewer 不显示会话操作和系统设置；Agent 不显示 SOP 和连接配置。后端仍执行权限校验。

`Ctrl/Cmd + K` 打开命令面板：

- 初始显示有权限页面快捷入口。
- 输入 2 个字符后调用 `GET /conversations?q=` 搜索联系人、会话 ID 和消息摘要。
- 选择结果跳转会话控制台并选中该会话。
- 不在前端索引或暴露无权限联系人。

通知按钮首期展示系统健康摘要和待接管数量，数据来自 `/health/details` 与 `/handoffs?status=pending`，不做独立通知中心。

## 4. 运营总览

### 4.1 筛选

控件：今天/7 天/30 天、渠道、Inbox、团队。默认最近 7 天和用户全部授权 Inbox。筛选参数同时传给 overview、trend、funnel 和 blockers，任一接口失败仅影响对应区块。

### 4.2 指标和下钻

| 区块 | API | 交互 |
|---|---|---|
| 核心指标 | `GET /bi/overview` | 点击 AI 接管进入会话列表对应筛选；点击人工待处理进入队列 |
| AI/人工趋势 | `GET /bi/trends` | Tooltip 显示分子分母，不只显示比例 |
| 客户旅程漏斗 | `GET /bi/funnel` | 点击阶段进入会话列表并附带阶段筛选 |
| 卡点分布 | `GET /bi/blockers` | 点击原因进入人工队列或会话列表 |
| 渠道健康 | `GET /health/details` | 异常项跳转系统设置对应区域 |

指标响应必须带 `definition` 和 `computed_at`。数据未聚合时显示“暂无可计算数据”，不能使用前端 Mock 值。

## 5. 会话控制台

### 5.1 列表

- `GET /conversations`，参数包含 page、q、inbox_id、ai_state、journey_stage、assignee_id、labels。
- 列表展示联系人、渠道、最后消息摘要、时间、最终 AI 状态、阶段和人工分配。
- 选择会话调用详情与历史消息；URL 保存 `conversation_id`，支持刷新恢复。
- 分页由服务端提供，删除当前前端全量数组过滤逻辑。

### 5.2 消息时间线

- `GET /conversations/{id}/messages`，向上滚动加载更早消息。
- 区分客户、AI、人工、SOP、私密备注和系统事件。
- 附件显示类型、名称、大小与安全下载入口。
- Chatwoot 原始正文不可编辑。
- “运营备注”首期调用平台备注 API；若选择同步为 Chatwoot 私密备注，界面必须明确标识并二次确认。

### 5.3 AI 控制

详情同时显示：本地会话配置、最终状态和状态原因。例如本级开启但 Inbox 关闭时，Toggle 保持本级配置，旁边显示“最终关闭：Inbox 已关闭”。

| 操作 | API | 规则 |
|---|---|---|
| 开启/关闭会话 AI | `PATCH /conversations/{id}/ai-state` | 使用 version；客诉/人工标签存在时不能直接开启 |
| 修改旅程阶段 | `PATCH /conversations/{id}/journey-stage` | 保存后同步 Custom Attribute，失败显示同步状态 |
| 刷新标签 | `POST /conversations/{id}/sync` | 从 Chatwoot 重新读取，不自动调用 AI |
| 打开 Chatwoot | 使用详情 `chatwoot_url` | 新标签页打开，不由前端拼接不可信 URL |

标签来自 Chatwoot，首期只允许通过明确业务动作增删系统标签，不提供任意自由编辑器，避免覆盖人工标签。

### 5.4 转人工

点击“转人工”打开 Modal：原因、优先级、目标团队、说明。确认调用 `POST /conversations/{id}/handoff`。

成功后：

- 最终 AI 状态立即显示人工接管。
- 未发送 AI/SOP 任务显示已取消。
- 显示 Chatwoot 标签和分配同步状态。
- 提供“查看人工队列”。

若版本冲突，关闭提交状态，刷新详情并提示“会话状态已被其他操作修改”。

### 5.5 联系方式

- 默认显示脱敏值；有权限用户点击“查看完整信息”调用受审计接口。
- 待确认联系方式显示确认/判无效按钮。
- 确认调用 `POST /lead-captures/{id}/confirm`，成功后才计入留资。
- 前端不可将完整联系方式缓存到 localStorage、日志或分析工具。

## 6. 人工接管队列

### 6.1 列表和筛选

`GET /handoffs` 支持 status、priority、reason、team、assignee、q、page。Tab 数量由服务端返回，不从当前页数组计算。

### 6.2 操作

| 操作 | API | 权限/结果 |
|---|---|---|
| 领取 | `POST /handoffs/{id}/claim` | Agent+；分配到当前映射的 Chatwoot User |
| 改派 | `POST /handoffs/{id}/reassign` | Supervisor+；选择平台用户/Chatwoot 坐席 |
| 完成 | `POST /handoffs/{id}/complete` | 当前处理人或 Supervisor；AI 仍暂停 |
| 恢复 AI | `POST /conversations/{id}/resume-ai` | Supervisor+；独立确认操作 |
| 打开 Chatwoot | 响应中的 `chatwoot_url` | 有 Inbox 权限 |

领取前 Modal 显示接管原因、当前团队和最新消息摘要。`409 handoff_already_claimed` 时自动刷新并显示实际领取人。完成 Modal 明确提示“不自动恢复 AI”。

## 7. SOP 配置

### 7.1 列表

`GET /sops` 展示状态、版本、适用 Inbox、渠道、入组数、成功送达、回复率、留资率和更新时间。草稿不能直接打开运行 Toggle；必须先通过发布校验。

操作：新建、编辑草稿、复制为草稿、查看版本、校验、发布、暂停、归档、查看执行记录。

### 7.2 编辑器

使用分步编辑，不在单个小 Modal 中承载完整 SOP：

1. 基础信息：名称、说明、适用 Inbox/渠道。
2. 进入条件：阶段、Conversation 标签、Contact 阻断标签、最后消息、留资状态。
3. 节点：相对等待、固定时间、条件、发送消息、更新阶段、转人工、结束。
4. 退出条件：客户回复、人工接管、客诉、已留资、拒绝联系、会话结束、策略关闭。
5. 频控：每日会话/联系人上限、最小间隔、归因窗口。
6. 校验与发布：显示错误、警告和预计适用会话数。

### 7.3 时间节点

- 相对时间：数值、单位、基准事件；最小 1 分钟。
- 固定时间：日期、时间、租户时区；界面同时显示换算后的 UTC。
- 周期检查：周期和执行时间，只创建符合条件且已有会话的 enrollment。
- 已过去固定时间不能发布；夏令时歧义必须要求用户选择具体偏移。

### 7.4 消息节点

选择消息类型后按渠道显示能力：

| 类型 | Facebook | Instagram | UI 行为 |
|---|---|---|---|
| 文本 | 可用 | 可用 | 显示长度计数 |
| 图片 | 可用 | 可用 | 上传、MIME/大小校验和预览 |
| 音频/视频 | P1 | P1 | MVP 标记后续，不允许发布 |
| 文件 | P1 | 不可用 | Instagram 选择时禁用 |
| 快捷回复 | P1 可降级 | 不可用 | MVP 禁用发布 |

文本加多个附件可能拆成多条消息，发布预览必须按实际发送顺序展示。媒体上传使用 `POST /media`，SOP 只保存 `stored_media_id`。

### 7.5 发布与执行

- `POST /sops/{id}/validate` 返回 errors、warnings 和 audience_estimate。
- errors 非空禁止发布；warnings 需要确认。
- `POST /sops/{id}/publish` 生成不可变版本。
- 暂停只阻止未提交任务，不撤回已提交消息。
- 执行明细展示 scheduled、blocked、submitted、delivered、failed、cancelled 及原因。

## 8. 系统设置

### 8.1 渠道与 Inbox

- 连接卡显示 Base URL、Account ID、Token 是否配置和最近测试结果，不显示 Token。
- “只读测试”调用 `POST /settings/chatwoot/test`，只访问 Account/Inboxes。
- “同步 Chatwoot”调用 `POST /settings/chatwoot/sync`，同步 Inbox、Agents、Teams、Labels。
- 全局和 Inbox AI/SOP 开关保存到本平台；关闭全局 AI 同时阻止新 SOP 自动发送，但不影响 Chatwoot 人工消息。
- 默认转接团队选项来自同步结果。

### 8.2 Account Webhook

- 页面读取现有 webhook_id、URL、订阅事件和最近接收时间。
- 必选事件不可取消：`message_created`、`message_updated`、`conversation_updated`、`conversation_status_changed`、`contact_updated`。
- 建议事件：`conversation_created`、`contact_created`。
- 保存调用 `PUT /settings/chatwoot/webhook`，由后端创建或更新 Chatwoot Webhook。
- 验证回调检查最近事件和连接健康，不伪造 Chatwoot 请求。
- Agent Bot 移至“高级/后续能力”只读说明，不提供启用 Toggle。

### 8.3 AI HTTP 接口

配置 Adapter 类型、Endpoint、鉴权类型、Token、超时和 Mock 规则。Token 输入保存后清空，只显示已配置和末四位。测试调用只使用示例上下文，不向真实客户发消息。

### 8.4 人工接管与标签映射

- 原因映射到优先级和默认团队。
- 系统语义映射到现有 Chatwoot 标签；保存前检查标签存在。
- Conversation 标签与 Contact 标签分区显示。
- 不允许同一标签同时映射到互相冲突的语义。

### 8.5 用户与权限

- 同步 Chatwoot Agents 只更新候选映射，不自动创建可登录用户。
- 创建平台用户需要邮箱、角色、Inbox 范围和可选 Chatwoot User 映射。
- 停用用户立即撤销全部 Session，不删除历史审计。
- 修改角色和范围必须确认，并写审计。

### 8.6 审计与健康

审计支持日期、操作者、动作、资源筛选；敏感前后值保持脱敏。系统健康展示 API、Worker 心跳、SQLite、Chatwoot、AI Adapter、最后 Webhook 时间和死信数量。

## 9. 前端类型边界

前端类型从 API Schema 生成或集中定义，禁止页面各自猜测 Chatwoot 字段。核心类型：`CurrentUser`、`InboxBinding`、`ConversationSummary`、`ConversationDetail`、`EffectiveAiState`、`HandoffTask`、`SopDefinition`、`SopVersion`、`AuditEntry`、`ApiError`。

Chatwoot 原始 payload 只在后端调试接口中由 Super Admin 查看，普通前端模型不包含 Token、Webhook Secret、完整 payload 或未授权联系方式。

