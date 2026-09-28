# 开场图片上传失败修复

2026-09-28 线上上传报 `Failed to fetch`。API 日志在 09:40、09:41 记录 `ops_api.upload_media → target.write_bytes → OSError: [Errno 28] No space left on device`。49 GB 系统盘可用空间为 0；未经处理的异常使跨域浏览器只能显示网络失败。

## 处理

- 清理 releases 下 15 份已有独立回滚备份的旧预检 `backend/data` 副本；保留当前正式数据、素材和全部 backups。首次清理恢复 3,266,994,176 字节可用空间。
- 上传遇到磁盘满或存储配额不足时返回 HTTP 507 和 `media_storage_full` 中文提示，清除本次未写完整的文件，不创建素材记录。
- 发布预检执行结束后清理其临时数据副本，成功和进程返回失败都执行清理。

## 验证与发布

- `backend/tests/test_opening_media.py`：15 项通过。包含磁盘满/配额不足、跨域错误响应、无残留文件与素材记录，以及正常上传和配置保存。
- 代码提交：`7998391ad7a4e89e5991daea95b7d24cf37d4d57`，已推送 main。
- 发布：`reply-consistency-20260928T014657Z`；三个服务正常。引擎保持 `reception-v2-agent-20260927-6a5fc4faac7acafe`。
- 线上 Playwright 操作公共接待页：新增临时图片消息，上传 14,121 字节 PNG；返回素材 101，浏览器图片预览完整解码为 640×360。
- 截图：`output/playwright/opening-upload-restored-20260928.png`。未保存测试开场；测试素材及短期测试登录会话已清理。
- 部署前后配置及环境哈希一致，客户出站记录保持 45。结束时可用空间 3,080,720,384 字节；确认本次预检副本已自动清理。
- 回滚位置：`/opt/china2go-ai/backups/reply-consistency-20260928T014657Z`。

本次不改开场文字、图片配置或客户回复逻辑，不调用模型。没有把本次上传成功当作开场内容业务验收通过。
