# 本地 AI 客服平台开发文档

最新客户接待决策边界与真实执行链路见 [模型主导的 AI 客户接待架构](./model-owned-customer-reception-architecture.md)。

当前运行边界：仅带 AI 标签且通过发送前检查的会话开放被动回复；SOP、沉默唤醒仍为演练。模型回放使用独立数据库并禁止 Chatwoot 写入。参见 [真实 Facebook 测试说明](./live-facebook-reply-test.md) 和 [完整上下文、超时与入组修复](./long-context-and-timeout-20260827.md)。

三板块原始演练版本见 [2026-08-26 三板块实现说明](./three-modules-implementation.md)。以下为此前基础设计，运行方式以以上最新说明为准。

SOP 新版时间规则、文字/图片/视频内容组及本地存储边界见 [SOP 时间与内容组](./sop-local-content-groups.md)。

版本：v1.0  
基线日期：2026-08-16  
适用阶段：本地 MVP 与 Chatwoot Cloud 联调

## 1. 技术基线

| 层级 | 技术 |
|---|---|
| 前端 | React 19、TypeScript、Vite |
| API | Python 3.11、FastAPI、Pydantic |
| 数据访问 | SQLAlchemy 2、Alembic |
| 数据库 | SQLite，WAL 模式 |
| 异步任务 | SQLite 持久任务表、独立 Worker |
| AI | Mock Adapter、HTTP Adapter、LangGraph 扩展协议 |
| 外部消息平台 | Chatwoot Cloud Application API 与 Account Webhook |
| 公网入口 | Cloudflare Named Tunnel |

固定开发地址：

- 前端：`https://ai.luoxuecong.asia`
- API：`https://api.luoxuecong.asia`
- Chatwoot Account：`180474`
- 首期 Inbox：`128859`
- Webhook：`https://api.luoxuecong.asia/v1/webhooks/chatwoot/{connection_key}`

`connection_key` 是不可预测的连接标识，不是 Chatwoot Token。所有 Token、密码和主密钥只通过本地环境变量或后端加密存储使用。

## 2. 文档导航

| 文档 | 内容 | 使用者 |
|---|---|---|
| [系统架构](./architecture.md) | 边界、进程、数据流、一致性与迁移边界 | 全体开发 |
| [前端交互规范](./frontend-interaction-spec.md) | 页面状态、按钮行为、权限和 API 映射 | 前端、产品、测试 |
| [后端设计](./backend-design.md) | FastAPI 模块、Worker、AI Adapter、安全 | 后端、测试 |
| [数据库设计](./database-schema.md) | SQLite 表、索引、状态和保留规则 | 后端、数据 |
| [API 契约](./api-contract.md) | 前后端接口、错误码、Chatwoot 映射 | 前端、后端、测试 |
| [本地联调手册](./local-integration-runbook.md) | 启动、Tunnel、Chatwoot 和 Facebook 测试 | 开发、运维 |
| [测试计划](./test-plan.md) | 单元、API、Worker、浏览器与真实渠道验收 | 测试、开发 |

产品边界与 Chatwoot 能力依据继续参考：

- [产品需求 PRD](../PRD-ai-customer-service-platform-v1.md)
- [Chatwoot 集成开发矩阵](../chatwoot-integration-development-matrix.md)
- [现有原型功能审查](../stitch-functional-audit.md)

## 3. 实施顺序

1. 建立后端工程、配置、SQLite、Alembic 和管理员初始化命令。
2. 完成 Auth/RBAC、Chatwoot 连接测试和资源同步。
3. 完成 Webhook 入库、Worker、会话镜像和标签实时同步。
4. 接入 Mock AI，跑通 Facebook 入站、AI 回复和人工阻断。
5. 将现有五个前端页面替换为真实 API，增加登录页。
6. 完成 SOP 调度、媒体发送、执行记录和基础 BI。
7. 按联调手册完成域名、Tunnel 与 Chatwoot Cloud 验收。

## 4. 明确不做

- 不修改 Chatwoot 源码。
- 不依赖 Chatwoot 付费 AI 或 Agent Bot。
- 不在前端保存 Chatwoot 或 AI Token。
- 不在本阶段实现旅游业务提示词、报价算法和知识库。
- 不把 SQLite 作为多实例生产数据库。
- 不允许 AI/SOP 使用 Meta Human Agent 窗口发送自动营销。
