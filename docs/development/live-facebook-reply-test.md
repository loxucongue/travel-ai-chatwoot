# Facebook 真实被动回复测试

更新：2026-08-26。本文替代上一份文档中“仅演练、不会实发”的运行状态说明。

## 当前上线范围

- 本地 API 与 `app.live_reply_worker` 负责真实被动回复，前端仍为 http://127.0.0.1:4175/#/conversations 。
- Chatwoot Account 180474、Facebook Inbox 128859。其他 Inbox 不参与本次真实回复。
- Chatwoot Cloud -> Cloudflare Relay/D1 -> 本地 Worker 拉取 -> DeepSeek -> Chatwoot 消息接口 -> Facebook。
- 不需要 Cloudflare Tunnel、Tailscale 或开放本地入站端口。本机必须开机、保持网络和 Worker 运行。
- 本轮只启用被动文字和图片。SOP、沉默唤醒、外部通知发送器不启动；已有 SOP 保持演练。
- 运行配置为 `APP_PROFILE=live_reply`、`OUTBOUND_MODE=live`、`CHATWOOT_WRITE_ENABLED=true`。旧发送 Worker 在这个 profile 下拒绝启动。
- 真实消息仅在通过权限、标签、近期入站、人工及文件检查后提交。不能用页面“模拟送达”证明 Facebook 已收到。

## 默认关闭与触发条件

准备时两遍扫描 Chatwoot，共183个会话。原先仅会话26带有 `ai`，已移除这一个 AI 标签，保留其他业务标签。所有现有本地会话的 AI 字段改为 disabled；未来无明确开启标签的会话也默认不回复。

默认人工不等于给所有会话增加“人工接管”标签或批量分配某位员工。本次没有改变真实客服分配。

测试步骤：

1. 在 Chatwoot 打开测试客户的**会话**（现有测试会话为26）。
2. 加上会话标签 `ai`。`AI` 大小写也识别，整标签必须匹配，不是 `ai测试`。不是在联系人详情单独加一个 Contact 标签。
3. 确认没有人工接管、客诉、AI关闭、拒绝联系、黑名单、已留资/已成交等阻断标签，也没有未完成的本地人工任务。
4. 从普通 Facebook 客户账号给同一公共主页发一条**新消息**。只加标签不会主动发消息，也不会补答旧消息。
5. 在 Facebook 查看返回文字和图片，在本地会话控制台查看 submitted、delivered/failed 等回执。
6. 移除会话 `ai` 标签，再发一条新问题，应不再自动回复。测试结束后移除标签。

本次默认关闭时间之前的入站、超过5分钟才处理的积压入站、私密备注、系统活动、出站回声均不自动回复。

同一轮连续客户消息会短暂合并。生成期间客户继续发消息、客服已公开回复、标签/分配/策略版本改变，会取消旧结果；每张图片发送前再核对云端最新状态。

移除 AI 标签会停止后续未提交的自动发送，不能撤回已经交给 Chatwoot 的请求。这是外部 API 的不可原子操作边界，不承诺绝对零毫秒竞态。

## 推荐真实问题

先测试完整线路，再问住宿和车辆，不要第一句就问价格：

1. 想看2027年桃花9日的行程圖片，不上珠峰。
2. 住宿房間有照片可以看看嗎？
3. 車子裡面長什麼樣子，有照片嗎？
4. 可以再發一次行程圖片嗎？

另一条线路可问：想看2027年桃花加珠峰11日行程圖。

资料不足问题（例如报价、资格、健康、其它目的地）会收到一次中性的顾问确认提示，并创建本地人工接管任务，停止后续 AI。这个提示不表示已经给外部员工发送了通知。要继续同一客户的 AI 测试，需要先处理人工任务，并由有权限用户显式恢复 AI；不绕过人工任务，仅重新加 `ai` 不能强制解锁。

## 不能声称已覆盖所有线路

真实开放的是2027桃花9日、桃花加珠峰11日的现有证据与12张已选素材。全部44张独立图片已归档，但剩余32张仍未开放自动发送。

冬游8/10日、其它目的地、具体价格/余位/实时团期、医疗及合同条件不能仅靠文件名当作完整知识。缺乏可核验资料的需求转人工，不用桃花图冒充其它线路。

图片是参考，不保证实际入住酒店、房型、车辆或氧气效果。图片的商业使用授权仍需企业负责。

## 发送与异常处理

- 本地图片通过 multipart `attachments[]` 上传给 Chatwoot，不需要 OSS 或公网图片地址。
- 文字及每张图片各有持久化业务幂等键，提交前先记录 submission_unknown。
- 成功创建消息只标记 submitted；后续事件更新送达/失败状态。
- 请求超时或进程在提交期间中断，不自动重发，等待人工对账，防止实际发成功后重复发送。
- 同一联系人本平台已提交过的同文件/同素材族图片会去重；明确请求重发允许再次提交。
- 目前不自动识别 Chatwoot 外部人工发过的相同图片，不声称覆盖全部人工附件去重。
- 图片被替换、文件缺失、未授权真实测试、线路不符，均在文字发送前拦截素材组；文字已提交后发生变化则停止剩余图片。
- 本期普通自动回复受可信客户消息后23小时55分钟保护，并要求 Chatwoot can_reply=true；AI标签不能重置窗口。Meta普通消息窗口说明见 [Meta Messenger 文档](https://www.postman.com/meta/messenger-platform-api/documentation/iyp204x/messenger-platform-api)。
- [Chatwoot附件发送接口示例](https://www.postman.com/chatwoot/chatwoot-apis/request/6e2o5ub/send-message-attachments)。

## 自测与审查

- 最终后端回归134项通过，1项付费模型测试默认跳过；该模型测试已另行显式执行并通过。云端中转5项测试、类型检查与部署构建通过。
- 修复了Relay只为message_updated区分内容版本、导致重复conversation_updated被丢弃的问题；conversation_updated、conversation_status_changed、contact_updated现在也按内容版本去重。已部署到原relay域名，不需要更换Chatwoot Webhook地址。
- 发送程序暂停并加单实例锁后，对真实测试会话26执行加ai/移除ai。Chatwoot -> Relay -> 本地开启约1.08秒、关闭约1.19秒；最后保持关闭，真实出站记录2条前后不变。这是本次实测时间，不是所有网络情况下的延迟保证。
- 自动测试使用隔离SQLite和模拟Chatwoot，不读取真实Token、不向真实客户发送。
- 覆盖未授权、人工标签、Contact阻断、人工任务、超时入站、缺失时间戳、新客户消息、客服回复、生成中删标签、重复事件、图片上传、缺图/错路线、未知提交不重发和回执同步。
- 额外单次真实DeepSeek测试识别了9日路线，并调用文字和图片发送接口的测试替身，模型耗时约2.31秒。端到端还包括合并等待、Relay轮询、Chatwoot状态读取和渠道发送，不能以2.31秒承诺Facebook最终到达时间。
- 代码审查关闭了旧发送程序、SOP和通用交付函数在live_reply模式下的执行入口，避免打开全局出站时同时启动主动营销。
- 人工任务必须先完成才可恢复AI；修复已有ai标签仍存在时，显式恢复没有更新本地disabled字段的问题。保留人工分配/标签管理接口，总开关和收件箱开关继续阻断真实回复。
- 审查补上“远端时间戳/标签状态未知即阻断”，不把无法读取状态当作允许发送。
- 最终Facebook文字/图片实收，需由用户按上述步骤发送新问题后验证；本轮没有代用户给真实客户发测试消息。

## 启停

工作目录 `E:\ai_code\travel_ai_Chatwoot\backend`，使用项目根目录的虚拟环境。实际入口：

```powershell
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
..\.venv\Scripts\python.exe -m app.live_reply_worker
```

不要同时运行 `app.worker_main` 或另一个Relay观察Worker。新Worker有单实例文件锁。

紧急停止：在本地关闭AI总开关/对应Inbox开关，或在Chatwoot移除目标会话ai标签。彻底关闭实发可停Worker后将环境恢复evaluation/disabled/false，再重启API。

只读诊断：`python scripts/prepare_live_reply.py`。`--apply` 是显式默认关闭重置操作，会移除现有会话ai标签；不是每次启动都执行。
