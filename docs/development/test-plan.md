# 测试与验收计划

## 1. 测试层级

| 层级 | 工具 | 数据/外部依赖 |
|---|---|---|
| 单元测试 | pytest | 临时 SQLite，无网络 |
| API 集成 | pytest + FastAPI TestClient/httpx | 临时 SQLite，Chatwoot/AI mock |
| Worker 集成 | pytest + respx | 临时 SQLite，模拟重启与超时 |
| 前端组件/构建 | TypeScript、Vite | API fixture |
| 浏览器 E2E | Playwright | 本地 API、Worker、测试数据库 |
| 真实渠道 | Facebook 测试账号 | Chatwoot Cloud、Tunnel |

每次测试使用独立数据库和上传目录，禁止连接真实 `data/app.db`。真实 Facebook 测试只能针对明确测试会话和测试标签。

## 2. 单元测试

### 2.1 AI 状态

- 租户关闭覆盖所有下级开启。
- Inbox 关闭覆盖会话 AI开启。
- Contact `拒绝联系/黑名单` 阻断全部会话。
- Conversation `客诉/人工接管/AI关闭` 阻断当前会话。
- `can_reply=false` 或窗口关闭产生 CHANNEL_BLOCKED。
- 标签变化只重算状态，不创建 AI Run。
- 未知标签不影响状态，写回时仍保留。

### 2.2 标签与幂等

- GET、合并、POST 不覆盖人工标签。
- 同一 message_created 重放只产生一个 message_event 和 ai_run。
- message_updated 的 submitted/delivered/failed 更新不会被粗粒度幂等吞掉。
- 本平台写标签产生的回流 Webhook 不形成循环。

### 2.3 RBAC 与脱敏

- 每个角色的页面权限、动作权限和 Inbox 范围。
- 无权资源返回 404，不泄漏资源存在。
- BI Viewer 不可读取完整 Contact。
- reveal 操作有权限时返回一次完整值并写审计。
- 停用用户后所有 Session 失效。

### 2.4 SOP

- 相对分钟/小时/天与固定时间转换 UTC。
- 过去时间、无退出条件、无频控、渠道不支持消息校验失败。
- 客户回复、人工、客诉、留资、拒绝联系正确退出。
- 同一 Contact 跨会话频控。
- 文本/附件拆分预览与实际计划一致。

## 3. API 集成测试

### 3.1 Auth

- 正确登录设置 Secure Session；错误登录不暴露邮箱存在性。
- 缺少/错误 CSRF 的写请求返回 403。
- 错误 Origin 返回 403。
- 过期、撤销 Session 返回 401。
- CORS 仅允许 `https://ai.luoxuecong.asia` 带凭证访问。

### 3.2 Settings

- Token 保存后响应和日志均无明文。
- 连接测试只调用只读 API。
- Inbox 同步只更新 Chatwoot 镜像，不改变 AI 开关。
- Webhook 创建/更新事件正确，必选事件不能删除。

### 3.3 Conversations/Handoff

- 服务端分页、搜索和范围过滤。
- 版本冲突返回稳定 409。
- 转人工事务后即使 Chatwoot 同步失败也保持 AI 阻断。
- 两人领取只允许一个成功。
- Complete 不恢复 AI；Resume 遇强阻断失败。

### 3.4 SOP/Media

- 草稿可保存，未校验不能发布。
- 发布后版本不可修改，编辑创建新草稿版本。
- 上传拒绝超限、错误 MIME 和路径注入。
- 已引用媒体不能删除。

## 4. Worker 测试

- API 入库后 Worker 可处理，API 不等待 AI。
- Worker 在领取后崩溃，租约过期后任务恢复。
- AI 超时按策略重试，超过阈值转人工或 no_action，不重复发送。
- Chatwoot 429 遵循 Retry-After；401 进入连接异常，不无限重试。
- Create Message 结果未知时不盲目重试，先按幂等记录和消息查询对账。
- AI 生成期间人工接管或状态版本变化，结果 discarded。
- 发送前发现客户新回复，SOP job cancelled。
- Worker 心跳中断后 Health 显示 degraded。

## 5. 前端 Playwright

视口：桌面 `1440x900`、移动 `390x844`。

### 5.1 登录与全局

- 登录、退出、会话过期回跳。
- 无权限导航隐藏，直接访问显示 403 状态。
- 命令面板键盘打开、搜索、跳转和 Escape。
- 页面无文本重叠、横向溢出和不可点击遮挡。

### 5.2 五个业务页

- Overview：筛选刷新、区块独立错误、指标下钻。
- Conversations：分页、详情、历史加载、AI 状态原因、转人工冲突。
- Handoff：Tab 数量、筛选、领取冲突、完成不恢复 AI。
- SOP：分步编辑、渠道能力禁用、发布错误/警告、执行状态。
- Settings：连接、同步、Webhook、AI、标签、权限、审计和健康状态。

每页覆盖 loading、empty、error、forbidden、success。按钮执行时尺寸不得变化，长名称和错误文本不能破坏布局。

## 6. Chatwoot/Facebook 实测矩阵

| 场景 | 成功信号 |
|---|---|
| 普通 incoming | 一次 Webhook、一次 AI Run、一次回复 |
| outgoing/external echo | 只记录，不调用 AI |
| private note | 不调用 AI，不发送客户 |
| 重复 Webhook | duplicate=true，无第二次回复 |
| Conversation 标签关闭 | 3 秒内最终状态关闭，后续 incoming 被跳过 |
| Contact 拒绝联系 | 所有关联会话 SOP 阻断 |
| 转人工 | 标签、团队和本地任务一致；AI 零回复 |
| 文本 SOP | 到期且通过复核后送达一次 |
| 图片 SOP | multipart 提交并最终 delivered |
| 客户提前回复 | 到期任务取消 |
| can_reply=false | 不调用 Create Message，记录 channel_blocked |
| 24 小时窗口外 | 自动任务阻断，不尝试 Human Agent 绕过 |
| Chatwoot Token 失效 | Health 异常、任务有限重试、无数据泄漏 |

## 7. 安全检查

- 仓库、前端 bundle、Network 响应、日志中搜索现有 Chatwoot/Cloudflare/AI 密钥均为零结果。
- Cookie 具有 HttpOnly、Secure、SameSite 和正确 Domain。
- Webhook 未知路径、Account 不匹配、签名错误被拒绝。
- 上传不能访问任意本地路径，下载需要权限。
- 完整联系方式访问有审计，BI 导出默认脱敏。
- 错误响应不返回堆栈、SQL、环境变量和 Chatwoot 原始 Token。

## 8. 性能与可靠性基线

- Webhook P95 入库响应低于 500ms，最大不得超过 3 秒。
- 同一入站消息重复自动回复率为 0。
- Worker 重启后 pending/过期 lease 任务 60 秒内恢复。
- SQLite 在单 API、单 Worker 下连续 1,000 个模拟事件无锁死和丢失。
- 列表默认 25 条，P95 API 响应低于 1 秒（不含外部同步）。
- AI 自动回复端到端中位数目标 10 秒内，超时不阻塞 Webhook。

## 9. MVP 验收门槛

以下全部通过才允许连接真实业务 Inbox：

1. 标签修改后 3 秒内停止 AI。
2. 同一入站消息最多一次 AI 回复。
3. 人工接管状态 AI/SOP 误回复为 0。
4. SOP 阻断条件违规发送为 0。
5. Create Message 200 不被错误统计为 delivered。
6. Token 不出现在浏览器响应、前端构建和普通日志。
7. RBAC 在后端有效，跨 Inbox 越权测试全部失败。
8. Worker 重启不丢 Webhook 和到期 SOP。
9. Facebook 文本、图片、人工和标签闭环真实通过。
10. 所有失败任务都有错误码、请求 ID、重试或死信状态。

