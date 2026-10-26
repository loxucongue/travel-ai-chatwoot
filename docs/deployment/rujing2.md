# 第二家公司拆分与部署

日期：2026-10-26。维护分支：`codex/rujing2`。基线：`90cbd6f`。

## 仓库分析与拆分边界

现有系统是 React/Vite 控制台 + FastAPI + SQLite/Alembic + 两类 Worker，Chatwoot 为外部会话渠道。
业务既存在文件资料中，也存在默认配置、Prompt、Skill、产品分组和出站说明中，因此不能只删除图片。

| 层级 | 处理 |
| --- | --- |
| 登录、权限、CSRF、会话、审计 | 保留 |
| V3 调度、交付、重试、人工接管、顾问分配 | 保留 |
| 素材管理、官网知识、产品导入与配置页面 | 保留，并支持空目录初始化 |
| 原公司线路、图片、价目、知识与 Skill | 移入 `businesses/china2go`，不打包到新服务器 |
| 接待身份、默认话术、产品分组、特有报价说明 | 从平台代码提取到业务包 |
| 原账号 ID、域名、前端演示数据、部署入口 | 新实例清空或替换；生产构建排除旧演示数据 |
| 数据库、上传文件、密钥、服务器服务 | 全新独立资源，不复制原公司数据 |

这次保留了旅游线路的数据模型和行业通用逻辑，没有把系统重写为通用多行业 SaaS。
新公司名称、产品、业务政策、正式域名、模型及 Chatwoot 账号仍待提供；默认留资、人数问询、沉默跟进和对客发送关闭。
本轮未调用付费模型，也未向客户发送消息。

## 原资料提取

完整归档：`output/business-extract/90cbd6f/china2go-business-complete.tar.gz`。
校验清单：同目录 `manifest-complete.json`。包含 702 个文件，116,828,083 字节，覆盖中文文件名。
归档由 Git 冻结提交生成，逐文件及整包计算 SHA-256；不包含 `.env`、数据库和客户历史。
提取工具：`python scripts/extract_business.py --ref 90cbd6f`；已有归档时拒绝覆盖。

`main` 未修改。旧公司继续用原分支；本分支用于新公司。平台通用修复可以选择性 cherry-pick，业务目录和部署配置不跨公司合并。

## 服务器布局

目标：Ubuntu 24.04，`47.82.3.154`，2 vCPU / 约 4 GiB / 50 GiB。
访问入口：`http://47.82.3.154/`；API 同源 `/v1`，后端仅监听 `127.0.0.1:8012`。
未提供域名，因此当前仅部署 HTTP；正式使用前配置域名证书并设置 `SESSION_COOKIE_SECURE=true`。

```text
/opt/rujing2-ai/
  current -> releases/rujing2-<UTC时间>/
  releases/                       # 代码、前端和新公司业务包，含 SHA-256 manifest
  venv/                           # Python 环境
  shared/backend.env              # 独立配置与密钥，权限 0640
  shared/data/app.db               # 新公司数据库
  shared/data/uploads/            # 新公司上传文件
  shared/data/route-packages/      # 后台发布的产品覆盖包
  shared/initial-admin.json        # 初始化凭据，仅所有者可读
```

`rujing2-api` 和 `rujing2-playground` 已启用并随开机启动，使用专用系统用户 `rujing2`。
`rujing2-worker` 的 unit 已安装但不启用，须完成新公司模型、渠道和业务配置后再开启。
不部署 Cloudflare Relay；保留源代码并清除旧 D1 ID 和域名，必要时申请新资源。

本轮已验证首次改密；当前管理员账号及密码交付在本机忽略文件 `output/deploy/rujing2/admin-credentials.json`。
`initial-admin.json` 只保留初始化记录，首次改密后其中原密码不再有效。凭据不提交到 Git。

## 日常发布

1. 在 `codex/rujing2` 修改代码及 `businesses/rujing2`，运行相关测试、前端 `npm test` 和 `npm run build`。
2. `python scripts/deploy_rujing2.py package` 检查构建产物不含旧公司品牌、域名和账号，并生成白名单发布包。
3. `python scripts/deploy_rujing2.py deploy --server-note <私有服务器说明文件>` 发布至固定新服务器；不会读取 `.env.deploy`。
4. `python scripts/deploy_rujing2.py check --server-note <私有服务器说明文件>` 检查服务和数据库。

发布采用版本目录和 current 链接，保留 shared 数据；迁移前使用 SQLite backup 保存一致副本。
当前依赖按 pyproject 约束安装，服务器实际包版本可能与本机不同，部署后仍需验证接口。
自动回退只切回旧代码，不恢复旧客户数据库；数据库迁移若不向后兼容，须在发布前单独设计迁移回滚。
首次部署没有旧版本可回退。初始化脚本遇到已有公司或用户时拒绝清库。

## 配置新业务

1. 修改公司名称和品牌展示，设置正式域名和 HTTPS。
2. 在服务器 `shared/backend.env` 配置本公司的模型 Key、URL、模型名；后台配置独立 Chatwoot 连接及收件箱。
3. 在产品管理导入本公司产品包，上传审核后的素材，在官网知识页面维护本公司的资料。
4. 调整 `businesses/rujing2` 的接待 Skill 和 Prompt，或通过后台维护开场白、接待、留资、跟进等配置。
5. 演练验证后配置实发账号、收件箱范围和 live rollout；再启用对客总开关、租户 AI 及 live Worker。

新产品缺少专用 Skill 文件时，使用通用产品 Skill 编译当前资料，不需要为每次产品导入修改平台代码。
空目录是合法状态；不存在旧线路自动回填或旧公司知识回退。

## 验证记录

- 新空库完整 Alembic 迁移、登录/CSRF、空产品与知识页面、配置保存。
- 新公司 Prompt、Skill、配置及产品目录不含原公司内容，发布白名单排除旧业务包和私有数据。
- 首个合成产品可发布并编译 Skill，不依赖旧目录；测试数据仅存在本机临时数据库。
- 相关权限、接管、顾问分配、通知、模型传输以及业务规则回归测试。
- 169 个相关后端用例均已通过（导入初始化修复后重跑隔离用例）；未运行全部历史业务回放集。
- 前端 7 项测试和生产构建；公网首页及健康检查、浏览器登录与后台页面。
- 实发渠道和真实模型回复未验证：新公司相应凭据尚未提供。
