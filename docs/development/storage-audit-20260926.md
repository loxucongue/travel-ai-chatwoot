# 项目空间评估（2026-09-26）

本次仅评估，未删除文件。新增本报告与低风险候选清单。

## 统计口径

- 根目录：`E:\ai_code\travel_ai_Chatwoot`。
- 原始扫描：153,607 个普通文件，文件长度合计 11.804 GiB；无目录读取错误。
- 跳过 2,873 个链接/重解析点，避免跟随依赖链接进入其他位置或循环统计。
- 这是逻辑文件大小，不是精确磁盘分配空间；硬链接、压缩及分配单元会影响实际释放量。以下空间均为估计。
- 根目录没有 Git 元数据，不能假定删除后的代码、历史记录能够从 Git 恢复。

## 占用分布

| 目录 | GiB | 说明 |
| --- | ---: | --- |
| output | 7.581 | 验收结果、模型回放、部署包、测试数据库；混有脚本及业务原始材料 |
| data | 1.414 | 主要是数据库备份，也有知识库、数据库 |
| chatwoot | 1.159 | 上游代码、依赖、本地 PostgreSQL 数据、缓存 |
| backend | 0.710 | 业务代码、数据库、上传文件、备份及回放 |
| .venv | 0.213 | 当前 Python 环境 |
| cloudflare-relay | 0.208 | 主要是依赖 |
| .playwright-cli | 0.202 | 浏览器调试日志、追踪记录、快照 |
| frontend | 0.140 | 主要是依赖 |
| .runlogs | 0.087 | 日志及一份测试数据库，不能全按日志处理 |
| chatwoot-develop.zip | 0.070 | 原始源码压缩包 |

## 第一批：低风险清理候选（约 285.83 MiB）

应在相关开发服务、测试和浏览器追踪结束后清理。删除日志会失去对应诊断记录；需要留证的先归档。

| 范围 | MiB | 后果 |
| --- | ---: | --- |
| .playwright-cli/console-*.log 与 traces/ | 200.47 | 清除历史浏览器控制台日志与追踪记录 |
| .runlogs/ 下的 .log 文件 | 31.40 | 清除历史运行日志，保留其中数据库及其他文件 |
| backend 内 __pycache__、.pytest_cache 及根 .pytest_cache | 8.73 | 下次运行自动重建 |
| chatwoot/tmp/cache | 44.05 | Rails 缓存重新生成；首次运行可能较慢 |
| frontend/dist 与 .tsbuildinfo | 1.18 | 下次预览或发布前需要重新构建 |

逐文件列表见同目录 `storage-cleanup-candidates-20260926.csv`。这是评估时的候选快照，不是已经执行的删除记录；执行时须重新核对路径、文件变化与占用情况。

## 第二批：空间收益大，但先确定保留范围

1. **旧验收大文件：约 2.85 GiB。** `output/v2-completion-20260920` 总计 4.71 GiB，其中编号 1–26 的 `journeys-final-*`、`acceptance-final-*.json`、`acceptance-final-*.rows.jsonl` 合计 2,916.68 MiB。它们是历史模型运行证据，重新运行并不能重现相同结果。建议先压缩归档到其他磁盘，保留最新完整验收、失败摘要、需求台账、部署回执和源码快照。不能仅按最大编号认定某次验收已通过。
2. **历史部署包：约 1.74 GiB。** `output/deploy` 有 111 个 `.tar.gz` 发布包，合计 1,776.54 MiB。按已部署版本及可用回滚版本保留，归档其他包。不要删除整个目录：`scripts/deploy_review_fixes_20260916.py:14`、`scripts/deploy_reception_v2_preview_20260916.py:14` 从这里导入部署代码。
3. **数据库备份：共 1.76 GiB。** `data/backups` 有 31 个文件、1,418.11 MiB；`backend/data/backups` 有 22 个文件、380.59 MiB。建议按数据库来源及迁移节点分类，至少保留各来源的近期有效备份、重要迁移前备份，再归档或淘汰多余历史。未检查这些备份的数据库完整性，不能依据日期认定新备份能够替代全部旧备份。
4. **六份测试数据库候选：486.11 MiB。** 见下方清单。用途根据文件位置、命名及相关测试上下文判断；删除前仍需核实没有运行进程使用、没有独有人工复核数据。SQLite 的配套 WAL/SHM 不能在运行中单独删除。

六份数据库候选：

- `output/delivery-consistency-20260913/acceptance.db`：137.57 MiB。
- `output/business-contract-20260914/acceptance.db`：128.79 MiB。
- `output/advisor-feedback-ui.db`：80.18 MiB。
- `output/image-quality/asset-import-test.db`：49.85 MiB。
- `.runlogs/config-buttons-qa.db`：56.59 MiB。
- `backend/data/playground-final-qa.db`：33.12 MiB。

上述四类合计约 6.81 GiB，是待筛选范围，并非承诺全部可删除或可释放的空间。归档到同一磁盘不等于释放磁盘空间；文本压缩可能节省空间，但本次未实测压缩率。

## 第三批：可重装依赖，不是首选

| 范围 | MiB | 影响 |
| --- | ---: | --- |
| chatwoot/node_modules | 421.14 | 再开发本地 Chatwoot 前需要重新安装依赖 |
| cloudflare-relay/node_modules | 212.06 | Relay 开发、测试或部署工具需要重新安装 |
| frontend/node_modules | 141.54 | 当前前端无法直接启动或构建，需重装 |
| .venv | 约 218 | 当前后端无法直接启动，需重新建立 Python 环境 |

这些依赖合计约 0.97 GiB，但日常开发还会装回来。当前空间重点是历史结果和备份，不建议优先拆除开发环境。未验证依赖可以按现有声明完整重建。

`chatwoot/vendor` 中还存在约 395 MiB 的 Ruby 安装内容和 93 MiB 的本地 gem；需要核对安装方式、Gemfile 引用和本地修改后才可判断，不按普通缓存处理。

## 应保留

- `backend/app`、`backend/tests`、迁移脚本、`frontend/src`、`scripts`、`docs`、依赖清单与锁文件。
- `backend/data/app.db`（119.83 MiB）、`backend/data/uploads`（107.98 MiB）、`data/knowledge`、配置与密钥文件。
- `data/app.db` 是另一份数据库，不能因为与后端数据库重名就判定重复。默认数据库路径是相对路径，启动位置会影响实际读取位置（`backend/app/config.py:23`）。
- `backend/data/replays`、最新验收和部署证据、业务原始素材。
- `chatwoot/.local_pgdata`（77.48 MiB）是数据库目录，不能当缓存删除。
- `cloudflare-relay/.wrangler` 可能含本地持久状态，不整体按缓存处理。
- `chatwoot/vendor/db/sentiment-analysis.onnx` 是模型文件，不是数据库备份。
- `chatwoot` 源码、`chatwoot-develop.zip`、`output` 下候选源码和原始参考材料：本次没有进行逐文件差异和唯一性验证，不直接判定可删。

## 验证边界

已完成只读目录统计、项目说明与配置检查、主要部署脚本依赖检查。未删除或移动项目文件，未修改业务代码，未运行功能测试，未执行数据库完整性检查或备份恢复演练。
