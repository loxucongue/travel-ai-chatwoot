# Chatwoot 旅游 AI 离线评测后端

## 安全边界

评测环境必须保持：

```env
APP_PROFILE=evaluation
OUTBOUND_MODE=disabled
CHATWOOT_WRITE_ENABLED=false
```

在该配置下：

- Chatwoot 只允许 `GET/HEAD` 请求。
- 自动回复 Worker 不创建 AI Run 或 Outbound Message。
- SOP 到期任务标记为 `outbound_disabled`。
- DeepSeek 结果只写入 `evaluation_results`。

不得为了测试临时把 `OUTBOUND_MODE` 改成 `live`。

## 初始化

```powershell
cd E:\ai_code\travel_ai_Chatwoot\backend
python -m pip install -e ".[dev]"
python -m alembic upgrade head
```

`.env` 中配置 `DEEPSEEK_API_KEY`。密钥不会通过 API 返回，也不会写入模型调用日志。

## 启动

API：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

评测 Worker：

```powershell
python -m app.evaluation_worker
```

不要在评测期间启动 `app.worker_main`。

## 操作顺序

所有写接口要求管理员 Session、CSRF Token 和合法 Origin。

1. `POST /v1/evaluation/chatwoot-sync` 创建只读历史同步任务。
2. `GET /v1/evaluation/chatwoot-sync/{id}` 等待任务完成。
3. `POST /v1/evaluation/datasets` 构建桃花相关数据集。
4. `POST /v1/evaluation/runs` 创建 DeepSeek 离线评测。
5. `GET /v1/evaluation/runs/{id}` 查询进度。
6. `GET /v1/evaluation/runs/{id}/results` 查看客户问题、历史人工回复和 AI 草稿。
7. `PATCH /v1/evaluation/results/{id}/review` 保存人工复核。

安全状态可通过 `GET /v1/safety/outbound-status` 查询。

## 验证

```powershell
python -m pytest -q
```

评测前后应确认 `outbound_messages` 数量不变。Chatwoot 的非读取请求会在网络调用前抛出 `chatwoot_write_blocked`。
