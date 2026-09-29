# 590交通事实与留资时机修订发布

2026-09-29，用户明确要求提交、部署后执行。此前效果测试中的未通过项目仍保留，本次发布不代表全部效果验收通过。

## 发布结果

- 代码提交：`e1fd12de2bc7de1648362f2dec4624820f6f5500`，已推送GitHub main。
- 生产发布：`v3-consolidation-20260929T093322Z`。
- 实际引擎：`reception-v3-ba07c604e3a0f269`，与最终连续对话候选一致。
- 三个服务 `china2go-api`、`china2go-worker`、`china2go-playground` 均active，健康接口200。
- 297个V3会话绑定新发布号；旧任务状态计数未变化，没有重放历史消息。
- 真实出站45→45。发送保持开启，生产主动沉默开关保持关闭。

## 实际改动

发布包与生产比较，仅5个文件变化：V3系统提示、通用Skill、9日与11日Skill、通用话术配置迁移脚本。没有前端或线路资料变化。

通用Skill补充青藏铁路方向、林芝集合、成都交函等DOCX业务事实；留资从“一次后不再问”改为根据对话进展判断再次邀请，连续答疑不机械追索。模型最终文案仍直接使用，没有新增审核模型或文字替换脚本。

配置迁移实际仅改变 `common_scripts`：

| 场景 | 变更字段 |
|---|---|
| contact_family | name、scenario |
| contact_other_dates | scenario |
| contact_email_alternative | name、scenario |

正文、启停状态、其他自定义场景、开场、线路SOP、发送间隔、沉默配置、环境密钥均保持原值。

## 验证

- 发布前：相关工程测试37项通过；16条连续对话、111条客户消息，结果与失败详见[效果报告](v3-long-conversation-effect-review-20260929.md)。
- 发布包隔离预检：数据库兼容，依赖无差异，网络及出站关闭。
- 发布后：使用只读数据库、禁止网络的独立进程实际编译并加载三份Skill，验证铁路/集合/交函事实进入运行时，接待与SOP配置匹配已测候选（沉默计时测试值与生产值单独区分）。
- 逐字段验证仅上述3项场景名称/适用说明改变。开场、SOP、正文和沉默配置未变。
- 本次发布没有追加真实模型调用；此前累计测试账本仍为¥15.11761696，没有真实客户渠道或设备收件测试。

已知未解决：自由答复仍可能擅自分配五人房型、推断住宿卫浴，部分表达冗长，自报已加好友缺少完整承接状态。未将这些问题标记为已修复。

## 备份与回滚

完整备份：`/opt/china2go-ai/backups/v3-consolidation-20260929T093322Z`。

其中 `app.db` 为发布前数据库备份；`backend.env` 为原环境配置；代码目录与 `bindings.json` 用于恢复前一版本。通用话术配置另存为 `reception-config-before-contact.json`。

代码回滚可对本发布执行 `scripts/deploy_v3.py rollback`（先核对本地stage.json仍指向本发布；若已被后续发布更新，应指定本发布的远端release.py）。回滚脚本恢复代码和旧引擎绑定，不用旧数据库覆盖上线后的业务记录。配置需另行根据上述JSON恢复本次三个场景的name/scenario；恢复前比对是否有后续运营修改，不能无条件覆盖整个配置。

证据：`output/v3-consolidation-release/deploy-result.json`、`check-result.json`、`output/release-590-diff.json`、`output/release-590-smoke.json`。服务器发布目录另存 `deployment.json`、`post-config-smoke.json`。
