# China2Go AI Operations

当前 V2 使用线路 Skill 和后台话术配置，普通回复直接使用主模型输出。线路内容以官网第一、二条线路为准，见 [当前完整 Skills](docs/product/current-reception-skills-20260927.md) 与 [官网话术上线记录](docs/development/website-route-scripts-20260927.md)。

旧的审核式 V2 验收、临时部署与覆盖话术脚本已经移除，当前发布入口为 `scripts/deploy_reply_consistency_20260926.py`。旧设计文档保留作历史记录，不作为当前实现说明。

仓库提交范围、私有运行资料与新环境复现限制，见 [代码仓库管理说明](docs/development/repository-management-20260926.md)。

正式接待的进程职责、顾问手机提醒、健康检查和发布步骤，见 [正式接待运行说明（2026-09-26）](docs/development/live-runtime-operations-20260926.md)。下方本地启动段落为离线演练流程。

本项目通过 API 集成 Chatwoot Cloud。2026-09-26 已清理工作目录中的 Chatwoot 上游源码与依赖；本平台的前后端、Relay 和业务数据继续保留。历史数据与验收记录的压缩归档位于 `archives/storage-cleanup-20260926/`，具体清理范围及恢复说明见 [空间清理记录](docs/development/storage-cleanup-20260926.md)。

一期实现 Chatwoot Cloud 到本地 AI 客服运营平台的完整闭环。Chatwoot 负责 Facebook 收发和人工工作台，本平台负责 AI 接管、人工转接、SOP、权限、历史镜像、通知与业务 BI。

当前包含：

- Mock / 外部 HTTP AI Adapter 与发送前二次状态检查
- Chatwoot Agent、Team、Label 镜像，客服分配与标签双向同步
- 人工接管队列、SLA、站内通知和签名外部 Webhook
- 有限多节点 SOP、附件、标签/阶段触发、频控、演练与实发白名单
- 全量历史回填、暂停恢复、幂等更新和历史消息归因
- Admin / Supervisor / Agent、Inbox 数据范围、首次改密和联系方式脱敏
- 基于本地事件的运营总览、趋势、漏斗、转人工原因和 SOP 效果

## 本地启动

当前为三板块离线演练版本。保持 `APP_PROFILE=evaluation`、`OUTBOUND_MODE=disabled`、`CHATWOOT_WRITE_ENABLED=false`，不要启动客户发送 Worker。入口与验收记录见 [三板块交付说明](docs/development/three-modules-implementation.md)。

环境要求：Python 3.11、Node.js 22.12+（满足 Vite 要求，前端回归使用 Node 内置 TypeScript 支持）。命令均从项目根目录执行。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
Push-Location backend
..\.venv\Scripts\alembic.exe upgrade head
..\.venv\Scripts\python.exe -m app.cli create-admin --email admin@luoxuecong.asia
Pop-Location

# 终端 1
Push-Location backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 终端 2
Push-Location backend
..\.venv\Scripts\python.exe -m app.evaluation_worker

# 终端 3
Push-Location frontend
npm install
npm run dev -- --host 127.0.0.1 --port 4175
```

按以上命令启动后访问 `http://127.0.0.1:4175`，API 健康检查为 `http://127.0.0.1:8000/v1/health`。Evaluation Worker 只处理演练和显式创建的回放任务，不启动客户发送。

## 当前演练

登录后进入第二个导航“AI 演练场”，选择 Facebook Inbox 并新建演练。在被动对话中输入模拟问题，生成的是本地草稿；“模拟送达”仅改变演练状态。SOP、唤醒也只操作隔离任务，不会发送给客户。

“回放与资料”提供全量历史回放、人工复核和原始素材绑定。已有真实历史只作为只读快照使用。

## 历史联调流程（当前禁用）

1. 登录后进入“系统设置”，输入 Chatwoot Cloud Base URL、Account ID 和服务账号 API Token。
2. 依次执行“保存”、“只读测试”和“同步资源”，再为目标 Inbox 打开 AI。
3. 进入 “Account Webhook” 保存必选事件。公网联调前需要将 `api.luoxuecong.asia` 通过 Cloudflare Tunnel 指向本机 `127.0.0.1:8000`。
4. 用普通 Facebook 账号向 Page 发送 `测试人员触发消息`，预期收到 `测试人员回复消息`。

以上真实收发流程仅作为原有集成记录，当前不可执行。重新实发需要单独授权、渠道验收及部署审核，不能只切换页面上的演练按钮。全量历史回填须手工启动。

Token 只通过设置页提交，后端加密保存；不要写入 `.env`、源码、日志或文档。完整设计和接口说明见 `docs/development/README.md`。

重新设计前端时，以 [`docs/stitch-redesign-product-spec.md`](docs/stitch-redesign-product-spec.md) 为产品与字段基准。该文档明确区分当前可用能力、Chatwoot 边界和需要补充后端的交互。

## 公开原型演示

公开演示使用 `frontend/.env.demo` 启用浏览器内存 API。它会免登录加载脱敏样例，页面操作只在当前浏览器会话中模拟，不会连接本地 FastAPI、Chatwoot 或企业 AI。

```powershell
Push-Location frontend
npm run dev:demo
npm run build:demo

# 仅在已登录 Wrangler 且需要更新公开站时执行
npx wrangler pages deploy dist --project-name china2go-ai-demo --branch main
```

演示站地址为 `https://china2go-ai-demo.pages.dev`。不要把真实 Token、客户资料或聊天附件加入 `demo-api.ts`。

## 验证

```powershell
Push-Location backend
..\.venv\Scripts\python.exe -m pytest tests -q
Pop-Location
Push-Location frontend
npm test
npm run build
Pop-Location
```

2026-09-16 的权限、会话、通知及接管修复包含数据库迁移。部署顺序与迁移注意事项见 [修复交付说明](docs/development/project-review-fixes-20260916.md)。
