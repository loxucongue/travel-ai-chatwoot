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

### 2026-10-29 通用修复同步

本分支已选择移植截至 `origin/main 105c45c` 的消息回调归属、渠道失败处理、统一渠道 AI、报价产品归属、对话去重、多人介绍交接和顾问通知等通用改动。
本轮只更新维护分支，未发布到服务器；发送和新业务功能默认关闭。
详见 [同步分析与验证记录](../development/rujing2-platform-sync-20261029.html)。
厦门资料已整理为可选公司资料包并导入本机独立演练环境，见 [现有资料与真实回复记录](../business/rujing2-xiamen-rehearsal.html)。[完整资料清单](../business/rujing2-line-intake-20261029.html) 保留为后续完善参考，不是首轮演练的前置条件。
币种已确认并在维护分支适配为 TWD：新公司报价使用通用金额字段和明确币种，不能在 `*_cny` 字段填台币，也不会自动换汇。实际线路价目仍待业务审核，未发布到服务器。

### 台币价格配置约定

- `businesses/rujing2/profile.json` 固定 `currency=TWD`；线路可省略顶层币种并继承公司设置。含金额的 `party_pricing`、`seasonal_pricing`、带指定团期价的 `departure_availability` 必须声明 `currency=TWD`。
- 人数价格使用 `base_price`、`legacy_offer_price`（可选），阶梯优惠使用 `discount_progression.amount_step`；折扣和独团加价表中的数值同样按台币使用。模板必须写 `NT${amount}` 或明确“新台幣”。
- 季节档位使用 `price`、`single_supplement`；指定团期使用 `price`。金额为正整数，折扣与独团加价为非负整数；字段没有汇率转换功能。
- 计算与日期工具返回 `currency` 和通用金额字段。原公司 CNY 历史字段继续兼容；TWD 策略混用 `*_cny`、币种与公司不一致、已审文字出现人民币金额时拒绝导入。
- 新公司模型回复与实际组装的文字还会检查错误币种；NT$、TWD、NTD、新台幣等常用表达进入既有报价核验。此检查针对文本，不识别图片或视频中嵌入的价格，素材中的金额需业务人工审核。
- 客户收款币种、账户和手续费由业务单独确认；本次修改不包含收款或支付接口。

业务优先使用 [厦门资料与回复演练](../business/rujing2-xiamen-rehearsal.html)；[完整可填写清单](../business/rujing2-line-intake-20261029.html) 和 [接待流程图](../business/rujing2-ai-workflow.html) 供后续完善。线上 AI 尚未开始接待。

### 厦门资料的本地真实模型演练

资料目录：`businesses/rujing2/campaigns/xiamen-20261029/`。保留 DOC 全部 35 条原问答和来源哈希，29 条可直接答复或调整表达，6 条配置实际转人工；关联 6 图 2 视频。独立资料包不会在空白新公司启动时自动安装。

在仓库根目录使用本项目虚拟环境运行（Windows Python 为 `.venv/Scripts/python.exe`）：

```powershell
.venv/Scripts/python.exe backend/scripts/rehearse_rujing2_xiamen.py prepare --output output/rujing2-xiamen-rehearsal --model-env backend/.env
.venv/Scripts/python.exe backend/scripts/rehearse_rujing2_xiamen.py run --output output/rujing2-xiamen-rehearsal --model-env backend/.env
.venv/Scripts/python.exe backend/scripts/rehearse_rujing2_xiamen.py serve --output output/rujing2-xiamen-rehearsal --model-env backend/.env --port 8013
```

- `prepare` 只用于首次初始化；数据库存在时拒绝覆盖。已有演练直接 `serve`，更新资料后先 `refresh`。批量 `run` 与网页 worker 不同时运行，避免同时推进会话。
- `--model-env` 只读取模型 Key、URL 和模型名。数据库、上传、运行时产品、登录用户独立；对客发送、Chatwoot 写入、实发 worker 均关闭。未配置模型 Key 时不能执行真实模型回放。
- `serve` 要求已有 `frontend/dist` 生产构建（同源 `/v1`）；启动本地 API 和演练 worker，监听 `127.0.0.1:8013`。页面为 `http://127.0.0.1:8013/#/playground`。
- 登录信息位于演练输出目录 `login.json`；完整结果为 `results.jsonl`；模型用量估算为 `cost.json`。默认演练模型估算上限 5 元人民币，与业务报价台币无关，且不等于供应商实际账单。已有账本保留，`--budget` 只用于创建新账本。
- 批量回放包含 35 FAQ 和 6 扩展场景，也可 `--cases hotel_material,food_video` 只重跑指定场景。报告记录实际动作、发送素材、模型调用及文本；它不替代业务最终审核。
- `refresh` 更新线路与接待规则；变更素材文件和绑定时使用新演练输出目录重新 `prepare`，不覆盖旧库。

2026-10-29：41 类真实模型场景分批完成，修正交接动作后复测；原始失败记录保留。页面登录、8 个素材预览接口、餐食视频播放、连续发送问题、实际酒店图文回复均已验证。相关后端测试分批通过（37、61、32 项，存在重复用例，不相加）；本轮未改前端代码。未将这次代码与资料部署到远程服务器。

本次同时修复两项平台行为：顾问称呼替换按公司开关控制；模型输出空回复却填有交接原因时触发既有协议修复，避免会话静默停在等待状态。

### 将厦门演练资料导入现有服务器

代码发布会携带资料包，但不会自动修改现有数据库。发布后单独执行：

```powershell
.venv/Scripts/python.exe scripts/deploy_rujing2.py deploy --server-note <私有服务器说明文件>
.venv/Scripts/python.exe scripts/deploy_rujing2.py install-xiamen --server-note <私有服务器说明文件>
```

`install-xiamen` 先检查当前公司、发送开关、资料哈希及配置冲突，再暂停 API 和演练 worker，调用 `scripts/install_rujing2_xiamen.py --apply`，最后恢复服务并检查健康状态。预检失败不会停止服务；导入失败也会恢复服务。

- 在现有数据库旁的 `backups/` 创建一致备份；保留管理员、密码、原渠道和会话。
- 导入 35 FAQ 对应的线路规则、8 个素材记录和专用沙盒收件箱。素材复制到持久共享目录，使用哈希命名，后续清理旧版本不会丢失素材。
- 保持全局停发和空实发收件箱，素材全部 `live_approved=false`；不配置 Facebook 或开启实发 worker。
- 同一资料重复安装不会新增重复记录；发现既有线路、同名素材或人工修改过的接待字段时拒绝覆盖。后续业务调整通过后台维护，不通过重装恢复默认值。
- 可在服务器 `current/backend` 目录运行 `runuser -u rujing2 -- /opt/rujing2-ai/venv/bin/python ../scripts/install_rujing2_xiamen.py`，省略 `--apply` 只做预检。
- 模型凭据仍使用服务器自己的 `shared/backend.env`；导入程序不会复制本地或原公司的模型密钥。

2026-10-30 只读检查：公网健康检查、管理员登录正常；服务器线路和收件箱为空，总开关关闭。原微信临时目录中的服务器说明文件已失效，因此本次代码发布和服务器导入尚未执行。已有本地演练已重新启动，重启后真实模型回复及酒店图片发送验证通过（模拟客户 #56）。导入相关 5 项测试通过，前端 8 项测试及生产构建通过，发布包已校验包含 8 个素材和导入脚本、不包含旧公司目录和私有配置。

## 验证记录

- 新空库完整 Alembic 迁移、登录/CSRF、空产品与知识页面、配置保存。
- 新公司 Prompt、Skill、配置及产品目录不含原公司内容，发布白名单排除旧业务包和私有数据。
- 首个合成产品可发布并编译 Skill，不依赖旧目录；测试数据仅存在本机临时数据库。
- 相关权限、接管、顾问分配、通知、模型传输以及业务规则回归测试。
- 169 个相关后端用例均已通过（导入初始化修复后重跑隔离用例）；未运行全部历史业务回放集。
- 前端 7 项测试和生产构建；公网首页及健康检查、浏览器登录与后台页面。
- 首次部署时未验证实发渠道和真实模型回复。2026-10-29 已完成上述本地真实模型演练；实发渠道仍未验证。
