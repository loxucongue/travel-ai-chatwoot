# Windows 本地联调手册

## 1. 目标拓扑

```text
Browser -> https://ai.luoxuecong.asia -> cloudflared -> 127.0.0.1:4175
Chatwoot -> https://api.luoxuecong.asia -> cloudflared -> 127.0.0.1:8000
FastAPI/Worker -> https://app.chatwoot.com
```

本机已具备 Node 20、Python 3.11 和 `cloudflared`。以下步骤不使用 Docker。

## 2. 后端环境

后端工程建立后，在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".\backend[dev]"
```

从 `backend/.env.example` 创建本机 `backend/.env`，填写值但不得提交。变量至少包括：

```text
APP_ENV
APP_SECRET_KEY
APP_ENCRYPTION_KEY
DATABASE_URL
FRONTEND_ORIGIN
SESSION_COOKIE_DOMAIN
SESSION_COOKIE_SECURE
CHATWOOT_REQUEST_TIMEOUT_SECONDS
AI_REQUEST_TIMEOUT_SECONDS
WORKER_POLL_INTERVAL_SECONDS
WORKER_LEASE_SECONDS
UPLOAD_DIR
MAX_UPLOAD_MB
LOG_LEVEL
```

Chatwoot API Token 和 AI Token 通过管理员页面或后端初始化命令写入加密字段，不写进前端 `.env`。本地密钥可用 Python `secrets` 生成，命令输出不得粘贴到文档或提交记录。

初始化：

```powershell
Set-Location .\backend
alembic upgrade head
python -m app.cli create-admin --email admin@luoxuecong.asia
```

确认生成：

```text
data/app.db
data/uploads/
```

## 3. 启动本地进程

分别打开三个 PowerShell 窗口。

API：

```powershell
.\.venv\Scripts\Activate.ps1
Set-Location .\backend
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Worker：

```powershell
.\.venv\Scripts\Activate.ps1
Set-Location .\backend
python -m app.worker_main
```

前端稳定预览：

```powershell
Set-Location .\frontend
npm install
npm run build
npm run preview -- --host 127.0.0.1 --port 4175
```

日常 UI 开发可用 Vite dev server，但 Chatwoot 联调期间建议使用 build/preview，减少热更新和开发代理变量。

本地检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-WebRequest http://127.0.0.1:4175/ -UseBasicParsing
```

预期 API 返回 `ok` 或带原因的 `degraded`，前端返回 200。

## 4. Cloudflare Named Tunnel

使用 locally-managed Named Tunnel。Cloudflare 官方要求带 ingress 的配置最后包含 catch-all 规则，并可使用 `ingress validate` 检查。

首次认证和创建：

```powershell
cloudflared tunnel login
cloudflared tunnel create travel-ai-local
cloudflared tunnel list
```

命令会在 `%USERPROFILE%\.cloudflared\` 产生 Tunnel credential JSON。该文件是密钥，不复制到项目目录，不提交。

在 `%USERPROFILE%\.cloudflared\config.yml` 配置：

```yaml
tunnel: <TUNNEL-UUID>
credentials-file: C:\Users\<WINDOWS-USER>\.cloudflared\<TUNNEL-UUID>.json

ingress:
  - hostname: ai.luoxuecong.asia
    service: http://127.0.0.1:4175
  - hostname: api.luoxuecong.asia
    service: http://127.0.0.1:8000
  - service: http_status:404
```

建立 DNS 路由：

```powershell
cloudflared tunnel route dns travel-ai-local ai.luoxuecong.asia
cloudflared tunnel route dns travel-ai-local api.luoxuecong.asia
```

验证配置与路由：

```powershell
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://ai.luoxuecong.asia
cloudflared tunnel ingress rule https://api.luoxuecong.asia/v1/health
```

运行：

```powershell
cloudflared tunnel run travel-ai-local
```

公网检查：

```powershell
Invoke-RestMethod https://api.luoxuecong.asia/v1/health
Invoke-WebRequest https://ai.luoxuecong.asia/ -UseBasicParsing
```

不要给 API hostname 配置 Cloudflare Access 登录页，否则 Chatwoot 无法投递 Webhook。后台前端可后续加 Access，但不能替代应用自己的 RBAC。

Cloudflare 参考：

- https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/create-local-tunnel/
- https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/

## 5. 平台首次配置

1. 打开 `https://ai.luoxuecong.asia`，使用 CLI 创建的管理员登录。
2. 系统设置 -> Chatwoot：填写 `https://app.chatwoot.com`、Account `180474` 和服务账号 Token。
3. 点击“只读测试”，确认 Account 和至少一个 Inbox 可读取。
4. 点击“同步 Chatwoot”，确认 Facebook Inbox `128859`、Teams、Agents 和 Labels 出现。
5. 开启租户 AI，并仅为 Inbox `128859` 开启 AI；SOP 先关闭。
6. AI 设置选择 Mock Adapter。
7. 配置 Mock 文本规则：触发“测试人员触发消息”，返回“测试人员回复消息”。

Token 保存后 UI 只能显示 configured 和末四位，浏览器 Network 响应不得出现完整 Token。

## 6. Chatwoot Account Webhook

平台生成的地址：

```text
https://api.luoxuecong.asia/v1/webhooks/chatwoot/{connection_key}
```

在系统设置保存 Account Webhook，订阅：

```text
message_created
message_updated
conversation_created
conversation_updated
conversation_status_changed
contact_created
contact_updated
```

后端调用 Chatwoot Webhooks API 创建或更新；也可以在 Chatwoot Cloud 设置 -> Integrations -> Webhooks 中人工核对。只保留一个当前平台 Webhook，避免同一 URL 重复订阅。

验证信号：

- 设置页显示 webhook_id、事件列表和固定 URL。
- 新事件到达后 `last_received_at` 更新。
- `webhook_events` 只产生一条资源事件，重复投递标记 duplicate。
- API 日志不输出完整载荷中的敏感信息。

## 7. Facebook 自动回复测试

前置：使用非 Page 管理员的普通 Facebook 测试账号给 Page 发消息，避免把 Page 自己发出的 external echo 当客户入站。

步骤：

1. 测试账号发送“测试人员触发消息”。
2. Chatwoot 收到 incoming 消息。
3. Webhook API 在 3 秒内入库并返回 202。
4. Worker 创建 AI Run，Mock 返回固定文本。
5. Worker 发送前重新读取 Conversation，状态仍为 AI_ACTIVE。
6. Chatwoot 创建 outgoing，Facebook 测试账号收到“测试人员回复消息”。
7. 平台消息状态先为 submitted，随后变为 delivered 或 failed。

失败排查顺序：Webhook 最近接收、Worker 心跳、AI Run、Conversation 最终状态、Chatwoot API 错误、message_updated 状态。

## 8. 标签实时阻断测试

1. 保持测试会话打开。
2. 在 Chatwoot 手工增加 `AI关闭` 或 `人工接管` Conversation 标签。
3. 平台收到 `conversation_updated`，3 秒内显示最终 AI 关闭及标签原因。
4. 客户再次发送触发文本。
5. 平台记录 incoming 和 skip_reason，但不创建 AI 回复。
6. 移除阻断标签后确认状态按 Inbox 策略恢复；标签变化本身不得触发 AI。

Contact 级测试：增加 `拒绝联系`，确认同一 Contact 的会话都禁止 SOP；若 Webhook 未包含完整标签，后台调用 Contact Labels API 补全。

## 9. 人工接管测试

1. 在平台会话页点击转人工，选择原因、P1 和目标团队。
2. 本地状态立即进入 HUMAN_HANDOFF，未提交 AI/SOP 任务取消。
3. Chatwoot 出现人工标签和团队分配。
4. 人工队列领取任务，Chatwoot Assignee 更新。
5. 客户继续发消息，AI 不回复。
6. 点击完成，任务归档但 AI 仍暂停。
7. Supervisor 单独执行恢复 AI，阻止标签不存在时才可成功。

并发测试：两个浏览器同时领取，只允许一个成功，另一个收到 409 并刷新领取人。

## 10. SOP 文本与图片测试

先使用 2 分钟相对等待，不使用 24 小时测试周期：

1. 创建只适用于测试 Inbox 和指定测试标签的 SOP。
2. 节点：等待 2 分钟 -> 发送文本 -> 结束。
3. 设置退出条件和每会话每日 1 条频控。
4. 校验、发布，确认创建 enrollment/job。
5. 到期前不发消息；到期后重新检查并发送。
6. 客户在到期前回复时，任务取消且记录 `customer_replied`。

图片：

1. 上传小于渠道限制的 JPEG/PNG 到平台媒体库。
2. SOP 节点引用 `stored_media_id`，不使用 Chatwoot Active Storage 临时 URL。
3. Worker 以 multipart `attachments[]` 上传 Chatwoot。
4. Facebook 收到图片，平台状态最终 delivered。

## 11. 停止与恢复

结束测试时先停止 Worker 和 API，再停止 cloudflared。不要删除 Named Tunnel 或 DNS；下次启动本地服务和 `cloudflared tunnel run travel-ai-local` 后地址保持不变。

数据库备份：

```powershell
Copy-Item .\backend\data\app.db ".\backend\data\backup\app-$(Get-Date -Format yyyyMMdd-HHmmss).db"
```

备份前停止写入或使用 SQLite 在线备份命令，不能在高频写入时简单复制并假设一致。

## 12. 常见故障

| 现象 | 检查 |
|---|---|
| Tunnel healthy 但 502 | 本地 4175/8000 是否监听、ingress service 是否正确 |
| API 公网正常但无 Webhook | Chatwoot URL、事件订阅、Access/WAF、connection_key |
| 收到 outgoing 又触发 AI | 必须只处理 `message_type=incoming` 且 `private=false` |
| 重复回复 | webhook/message 幂等键、Worker 租约、出站业务幂等 |
| 标签修改不生效 | 是否订阅 conversation_updated、事件是否入库、状态版本 |
| 图片失败 | MIME、大小、文件存在、multipart 字段和 Chatwoot 错误 |
| SQLite locked | 外部调用是否持有事务、WAL/busy_timeout、是否启动多个 Worker |
