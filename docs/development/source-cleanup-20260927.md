# 弃用代码清理与发布

本轮核对687个已跟踪文件的目录、源码引用、入口和脚本依赖；静态扫描覆盖其中456个文本文件。静态“无引用”只是候选，演练Worker由systemd启动，核实后保留。

删除16个文件：

- `backend/app/replay_patch.py`：2026-08-26回放修补入口，已无当前调用。
- `backend/scripts/apply_advisor_reception_defaults.py`：已完成的一次性默认配置改写。
- `backend/scripts/apply_meeting_route_content.py`、`update_business_brand_copy.py`：会把官网话术覆盖回旧口径。
- `backend/scripts/evaluate_reception_v2_preview.py`、`evaluate_v2_release.py`、`evaluate_v2_business_feedback.py`、`v2_acceptance_cases.py`：旧V2预览/审核式验收及旧业务期望。
- `backend/scripts/export_v2_release_candidate.py`、`publish_reviewed_v2_content.py`：旧审核式发布清单和一次性内容发布入口。
- `backend/tests/test_v2_acceptance_writer.py`：专门测试已删除旧验收脚本的文件写入方法。
- `scripts/deploy_reception_v2_preview_20260916.py`、`deploy_review_fixes_20260916.py`：依赖未提交临时脚本的旧部署入口，已由当前完整发布脚本替代。
- `frontend/src/pages/Unavailable.tsx`、`AiPlayground.tsx`、`frontend/src/data.ts`：未引用的占位页、旧演练页、Mock数据。

另删除19个全仓无引用函数，涉及弃用的话术裁剪、独立审核器预约数据、旧关键词判断、重复包装和旧回放辅助方法；移除其无用导入及旧页样式。当前10个Skill的内容未改。

保留：仍被入口调用的V1兼容代码与诊断接口、真实/演练Worker、数据库迁移、当前知识与素材、历史交付证据。两个原有未跟踪的客户导出脚本未修改、未提交、未部署。

发布脚本现在同步已跟踪的`backend/scripts`和`scripts`，确保服务器也删除弃用文件；不把本地未跟踪脚本打包。代码、脚本与线路资料随发布一起备份和回滚。

验证：后端全量回归1,424项通过、52项跳过、13条警告；前端测试和生产构建通过，中继5项测试通过。服务器隔离启动、数据库兼容及依赖检查通过。本轮没有重新调用付费模型，不向真实客户发送测试消息。正式发布结果在部署后补记。
