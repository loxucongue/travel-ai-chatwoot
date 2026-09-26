# 九项审查缺陷修复说明

对应审查：[project-review-20260916.md](project-review-20260916.md)。

## 修复内容

| 审查项 | 修复 |
|---|---|
| 1 通知越权 | 列表与标记已读共用权限谓词，同时匹配租户、Chatwoot 会话 ID 和 Inbox 授权；会话已不存在时对非管理员拒绝访问。无关联会话的广播仅管理员可见，个人系统通知仅收件人可见。 |
| 2 旧会话未撤销 | 管理员重置密码时，在同一事务撤销目标用户全部会话；自主改密撤销其他会话，保留发起改密的当前会话。 |
| 3 跨账号缓存 | 每次登录会话使用独立 QueryClient；切换时取消查询并清理旧实例，迟到的旧请求和回调无法填入新实例。 |
| 4 强制改密绕过 | 后端仅允许此状态访问本人身份、CSRF、改密和退出接口，其他已认证业务接口返回 password_change_required。 |
| 5 接管并发覆盖 | 条件 UPDATE 原子预占分配操作并递增版本，在提交后调用 Chatwoot；领取、改派与完成操作不能覆盖分配中的任务。 |
| 6 迁移约束缺失 | 新增客服绑定唯一约束；已有重复绑定会在任何结构变更前阻止迁移，不自动解绑账号。用户新增和修改的绑定冲突返回 409。 |
| 7 BI 统计越权 | 留资与成交事件查询应用 Inbox 授权及选定 Inbox；统计按事件时间，不因会话镜像更新时间较早而漏计。 |
| 8 广播共用已读 | 新增 notification_reads，以通知和用户为联合主键；重复标记幂等。未读过滤在分页前执行，未读总数覆盖全部可见通知。 |
| 9 测试目录依赖 | 修正文档命令，配置 pytest 的 backend 导入路径；根目录与 backend 目录均能正确收集测试。 |

## 接管分配失败与恢复

预占操作保存目标平台用户及当时的 Chatwoot 客服 ID。远程调用期间不持有 SQLite 写锁，任务保持 pending/claimed，因此原有人工接管对自动回复的阻断仍生效。

- 远端明确拒绝分配：释放预占并更新版本，刷新后可重新操作。
- 网络超时、服务端错误或进程中断：保留目标，阻止再次领取、改派和完成；界面显示“核对分配”。
- “核对分配”只读取 Chatwoot 最新会话，不重复发送分配请求。确认远端负责人等于保存的目标后才完成本地确认。
- 若远端负责人不匹配，操作人员应先在 Chatwoot 核对并分配给提示的目标客服，再点击“核对分配”。不会把不确定结果自动视为失败并重试。

新增接口：`POST /v1/handoffs/{id}/reconcile-assignment`，请求携带当前 `version` 与 CSRF。权限沿用会话访问限制。

## 数据库变更与部署

新迁移：`d2f6a8b901ce`，父版本 `d1e7a9c4b205`。

- `users.chatwoot_agent_id`：唯一约束，多个 NULL 仍然允许。
- `handoff_tasks`：新增 `assignment_target_user_id` 与 `assignment_target_agent_id`。
- `notification_reads`：新增按用户保存的阅读回执表。

历史个人通知的已读记录可准确确定读者，因此迁移保留。历史广播只有一个共享 read_at，无法确定实际读者，因此不推断个人已读状态，升级后按用户显示未读；通知正文保留。

发布时先停 API 与所有使用同一数据库的 Worker，使用 SQLite backup API 备份数据库，再在 backend 目录运行 `..\.venv\Scripts\python.exe -m alembic upgrade head` 与 `..\.venv\Scripts\python.exe -m alembic check`，成功后启动对应版本的服务。不要让新代码在旧表结构上运行，也不要在迁移时继续运行旧进程。

如果迁移报告 `duplicate_chatwoot_agent_bindings`，应按实际身份映射消除重复绑定后再迁移；不能随意选择一个用户自动解绑。迁移脚本会在改变结构之前停止，保留原记录。

本次只在隔离临时数据库验证迁移，没有升级现有业务数据库、重启运行中的服务或开启发送开关。

## 验证入口

本次验证结果：

- 根目录全量后端回归：1096 passed、4 skipped、13 warnings，372.36 秒。
- 全量启动后补充了两种用户绑定冲突场景；最终新增回归集单独执行：17 passed，包含迁移与安全用例。
- frontend `npm test`：缓存隔离回归通过，覆盖迟到查询及旧 mutation 回调。
- frontend `npm run build`：TypeScript 检查及 Vite 生产构建通过。
- 临时库已验证升级、降级再升级、Alembic 模型一致性、外键完整性和重复绑定阻断。
- backend 目录也已验证新增测试集可正常收集。

没有执行浏览器端到端测试或真实 Chatwoot 分配。接管外部调用使用模拟客户端；这些结果不替代部署后的渠道联调。

```powershell
# 项目根目录
.\.venv\Scripts\python.exe -m pytest backend/tests -q

Push-Location frontend
npm test
npm run build
Pop-Location
```

新增回归覆盖越权列表与写入、同 ID 不同租户、个人通知、个人已读、未读分页、密码重置与自主改密、强制改密、统计范围与事件时间、陈旧版本竞争、分配中竞争、未知分配核对、明确失败恢复、迁移往返、重复数据迁移阻断及前端迟到请求缓存隔离。
