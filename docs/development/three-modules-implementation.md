# 三板块本地演练交付说明

## 运行边界

- 技术栈沿用 React/Vite、FastAPI、SQLAlchemy/Alembic、SQLite WAL。
- 当前数据库 `backend/data/app.db`，升级前备份位于 `backend/data/backups/before_three_modules_20260826_121652.db`。
- 本次只读同步冻结 173 个会话、4,578 条消息。账号 180474，Facebook Inbox 128859。
- 安全开关保持 `APP_PROFILE=evaluation`、`OUTBOUND_MODE=disabled`、`CHATWOOT_WRITE_ENABLED=false`。
- 真实出站记录基线 2，人工任务基线 0。演练不增加这些记录，不改真实标签，不发外部通知。
- 实际发送 Worker 在当前配置下拒绝运行。未来受控提交边界单独封装，但不代表真实发送已经验收。
- 默认不订阅真实流量进行自动模型调用。按需启用的 `--observe-webhooks` 仅观察已验证事件并生成影子草稿，仍不能发送。

## 页面

| 入口 | 功能 | 数据与限制 |
|---|---|---|
| `/overview` | 真实 BI 首页 | 沿用业务事件；演练不写业务指标 |
| `/playground` | 被动对话、SOP 时间模拟、沉默唤醒 | 独立会话、虚拟时间、状态控制、草稿和耗时；所有发送均为模拟 |
| `/reply-policy` | 被动策略和记录 | 全局 / Inbox 开关、2 秒合并、最长 5 秒、积压 300 秒、版本冲突 |
| `/sops` | 草稿、发布版本、节点、受众和执行 | 固定审核内容；发布不会开启实发；原文件不存在禁止使用 |
| `/wakeup` | 策略、咨询周期、历史候选和记录 | 历史候选只读预览；选中策略后新建演练，周期冻结策略版本 |
| `/evaluation` | 全量回放及旅游素材 | 管理员可查案例上下文、参考答案、草稿、证据和人工评分；图片上传绑定 |

主管仅可操作授权 Inbox，不能发布策略；客服不能进入策略编辑或真实历史评测。演练会话按创建者隔离。公开 Demo 不接真实历史或付费模型。

## 三模块执行

### 被动回复

`公开 incoming -> 幂等 -> generation 更新 -> 2~5 秒合并 -> 状态检查 -> 统一决策 -> 再查 generation/策略 -> 草稿`

每个新客户消息都取消旧结果和未执行的旧 SOP / 唤醒。附件也属于新发言；不能解析时给出人工建议。影子环境超过 5 分钟的积压只同步，不补发。

知识、DeepSeek、字段校验由 `decision_service.py` 共用。资料来源与引用在 `decision_knowledge.py`；历史顾问回答不是事实真值。人数、出发时间分别记录原文证据。线路标题中的“六人小团”不等于实际同行人数；人数区间不会被截成单一数值。

模型单次最多 20 秒，整次决策最多 30 秒、最多 3 个请求，包括结构修复；认证或普通客户端错误不重试。网络调用前提交数据库事务，避免长时间持有 SQLite 写锁。

### SOP

发布生成不可变 `sop_versions`，修改草稿不会改写既有版本。相对入组、相对最后客户发言、相对前序确认、固定上海时间均可使用。后续节点依赖前序模拟确认；失败或跳过不继续假定已送达。

客户新回复默认退出；状态控制变化也按保守策略使旧任务失效。演练标签新增可以触发本地入组，历史标签不会自动补触发。手工入组和标签入组均去重。

暂停不消耗任务，恢复后仍受 15 分钟宽限与 24 小时入组有效期限制，不能借恢复越过渠道窗口。第一节点默认相对入组，后续可相对前序确认。

只支持文本、图片、视频、音频、文件的固定审核内容，不调用模型改写 SOP。Instagram 禁止文件。测试上传图只验证结构，不绑定为业务原图。

### 沉默唤醒

必须同时存在客户真实发言和我方确认回复；草稿不能作为送达锚点。默认最后确认回复后 2 小时评估。同一 generation 一个咨询周期，最多一条模拟触达。

模型结果是生成草稿、不联系、延后或建议人工。延后最多再评估一次，仍受有效期与渠道窗口限制。策略在周期创建时冻结；策略停用或变更后旧决策不直接执行。

历史候选页面不是可发送名单：显示渠道阻断、阈值未到等原因，真实发送还需最新状态核实。

## 协调与安全

- 优先级：人工/拒绝联系等阻断、新消息、被动回复、SOP、唤醒。
- 普通 Facebook / IG 自动消息单独按可信客户发言后的 24 小时窗口判断，保留 5 分钟余量，不把 `can_reply=true` 等同自动发送许可。
- 主动触达上海 09:00–21:00，SOP 与唤醒共享每联系人滚动至少 24 小时一次的预留；SOP 临近 10 分钟优先。
- 演练触达预留以隔离会话为范围；影子和未来真实执行使用联系人键，避免污染真实频控。
- SQLite 条件更新领取、唯一业务键和租约恢复；提交未知保持阻断，不按超时自动重发。只能以确切 Chatwoot Message ID 对账，否则人工核实。
- API 使用既有 Cookie、CSRF、Origin、RBAC 和 Inbox 范围校验。API 不返回密钥；新增评测和演练响应对电话、邮件、LINE/微信标识脱敏。未识别的自然语言隐私仍需上线前专项审查。

## 数据与接口

新增表：`reply_policies`、`automation_sessions`、`automation_runs`、`sop_versions`、`rehearsal_enrollments`、`rehearsal_jobs`、`wakeup_policies`、`silence_cycles`、`touch_reservations`、`sync_conversation_cursors`。不改变真实 outbox 的业务含义。

主要新增接口：

```text
GET/PATCH  /v1/automation/reply-policy
GET        /v1/automation/runs
GET/POST   /v1/playground/sessions
GET        /v1/playground/sessions/{id}
POST       /v1/playground/sessions/{id}/messages|advance|reset|confirm|sop
GET        /v1/sops/{id}/versions|preview
POST       /v1/sops/{id}/resume
GET/POST   /v1/wakeup/policies
PATCH      /v1/wakeup/policies/{id}
POST       /v1/wakeup/policies/{id}/publish|pause|resume
GET        /v1/wakeup/candidates|cycles|executions
POST       /v1/wakeup/simulate
POST       /v1/wakeup/cycles/{id}/exclude|confirm
POST       /v1/evaluation/runs/{id}/pause|resume|retry-failed
GET        /v1/evaluation/runs/{id}/report
GET        /v1/media/{id}/preview
POST       /v1/knowledge/assets/{id}/bind
```

已有评测、上传、SOP 和历史试用接口保留兼容。完整请求模型可查本地 FastAPI `/docs`。固定日期存 UTC，界面编辑按上海时间；本地浏览器不能自行改变发送时区。

## 启动与使用

在 `backend` 目录分别启动：

```powershell
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
..\.venv\Scripts\python.exe -m app.evaluation_worker
```

在 `frontend` 启动：`npm run dev -- --host 127.0.0.1 --port 4175`。登录沿用已有管理员，不会重置其密码。

1. 进入 AI 演练场，默认选 Facebook Inbox，新建演练，输入客户问题。
2. 查看草稿、证据、耗时；点击“模拟送达”建立确认回复锚点。
3. 推进 120 分钟可验证默认唤醒；人工开关、标签、客户新消息可验证阻断。
4. SOP 先保存草稿、校验并发布，在 SOP 时间模拟里选择发布版本入组，推进至节点到期。
5. 新建自定义唤醒策略后，在唤醒模式选择该策略，再新建演练。草稿策略不会自动评估，需管理员发布。

上述 Evaluation Worker 处理演练和显式创建的同步/回放任务。只想使用试用页时可增加 `--playground-only`，此时评测队列不会执行。也可手工运行 `python -m app.replay_cli sync|replay|report`，但不要同时启动两个回放处理进程。`replay` 使用冻结数据集幂等恢复，不会每次重建历史。修复影响的案例另有 `replay_patch`，其清单在报告目录保存。

## 验收资料

`output/three-modules-20260826/` 包含汇总、逐案例 JSON、目标重测清单、迁移安全检查和桌面/手机截图。该目录含业务信息，已排除出 Git，不部署到公开 Demo。

原始旅游图片、真实发送、真实标签接管时效、渠道实际送达和转化提升尚未验收。DeepSeek 能输出结构化草稿不代表事实完全正确或可以直接上线；所有未人工评分的案例保持待复核。
