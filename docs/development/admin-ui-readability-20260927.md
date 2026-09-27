# 配置页面可用性调整（2026-09-27）

## 修改

- 线路资料：删除双列大卡片及装饰图标，改为可搜索的单列资料列表；显示正文预览、发送顺序和附件数量；沿用现有抽屉编辑与发布接口。
- 价格与档期：纵向表单、清楚的内容标签、按内容行数展开文本框；资料来源折叠，保留编辑能力。
- 通用知识：左侧主题、右侧正文；手机使用主题下拉选择。搜索覆盖知识正文和常见问法，支持空结果。删除统计装饰卡、重复审核说明和内容指纹展示，来源按需展开。
- 区分内置资料与官网资料的启停范围；没有官网版本时仍可查看内置资料。
- 字体：移除 Geist 字体依赖，使用系统界面字体和中文无衬线回退；基础正文/表单 14px，辅助文字最低 12px，主标题 22px。实际 Windows Chromium 字体检查为 Microsoft YaHei UI。
- 清除废弃知识卡片/统计区域样式；修复手机侧栏关闭后阴影残留。

参考：
- https://ant.design/docs/spec/font/
- https://4x.ant.design/docs/spec/data-entry

## 验证

- 前端 TypeScript 检查与生产构建通过。
- 现有前端测试 1 项通过；git diff --check 通过。
- Playwright + 隔离数据库：线路资料搜索、抽屉编辑与恢复、价格编辑切换标签后保留、恢复后无脏数据；知识搜索/无匹配/主题切换/来源展开均通过。
- 1440px 桌面与 390px 手机检查；线路价格、知识库、公共接待、系统设置均无页面横向溢出。
- 截图：output/admin-content-desktop.png、output/admin-price-desktop.png、output/admin-knowledge-desktop.png、output/admin-price-mobile.png、output/admin-knowledge-mobile.png。
- 本轮只修改前端，不修改线路资料、话术、模型链路或后台配置。没有调用模型、发送客户消息。未重复后端全量回归；本轮没有在真实数据上执行资料发布操作。

## 发布

待填写部署结果。
