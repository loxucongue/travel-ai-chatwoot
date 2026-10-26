# Rujing2 AI 客服平台

`codex/rujing2` 用于第二家公司的独立开发和部署，基于原项目 `90cbd6f`。保留 React/Vite 控制台、FastAPI、SQLite/Alembic、V3 接待、素材和知识库、顾问分配、消息交付与 Chatwoot 集成能力。

当前部署是空白业务实例，不包含原公司的产品、素材、客户、会话、账号或模型密钥。公司名暂用 Rujing2，产品数据结构仍沿用旅游线路模型，跨行业业务需要另行适配字段和流程。

- 新业务：`businesses/rujing2/`
- 原业务隔离包：`businesses/china2go/`，仅供历史回归和追溯，不进入新部署
- 维护说明：[docs/deployment/rujing2.md](docs/deployment/rujing2.md)

## Local development

Python 3.11+, Node.js 22.12+. Install `backend[dev]` and frontend dependencies. Copy `backend/.env.example` to **`backend/.env.rujing2`**. This branch does not read the original `backend/.env`. From backend, run `alembic upgrade head` and `python -m app.cli create-admin --email your-admin@example.com`.

Run separate terminals in backend:

```text
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m app.playground_worker
```

From frontend: `npm run dev -- --host 127.0.0.1 --port 4175`.
Local rehearsal settings: `APP_PROFILE=evaluation`, `OUTBOUND_MODE=disabled`, `CHATWOOT_WRITE_ENABLED=false`.

默认数据库、上传文件、运行产品包在 `backend/data/rujing2/`，不会读取原来的 `backend/data/app.db`。Live Worker 在新公司的模型、渠道、业务配置验证前不启动。

## Verification and deployment

新业务隔离检查：`python -m pytest tests/test_business_isolation.py -q`。Frontend: `npm test` and `npm run build`.
发布：`python scripts/deploy_rujing2.py package`，然后 `python scripts/deploy_rujing2.py deploy --server-note <私有服务器说明文件>`。本地需安装 `paramiko` 并预先记录 SSH 主机公钥。旧 `deploy_v3.py` 在本分支禁止执行。

历史回归测试显式使用 `china2go` 业务包；新业务测试在独立进程和临时数据库中运行。模型验证须另配本公司的凭据。

历史业务脚本及文档保留作追溯，原路径和生产环境假设不能直接用于新部署。
