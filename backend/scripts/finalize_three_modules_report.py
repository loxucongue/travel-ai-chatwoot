"""Build the human-readable acceptance report without making model/network calls."""
import json
from pathlib import Path
from sqlalchemy import select
from app.db import SessionLocal
from app.models import EvaluationRun,EvaluationDataset,MaterialAsset
from app.evaluation_service import refresh_run
from app.replay_cli import export_report


def main():
    target=Path('../output/three-modules-20260826')
    with SessionLocal() as db:
        run_ids=db.scalars(select(EvaluationRun.id).join(EvaluationDataset).where(EvaluationDataset.name.like('three-modules-20260826-%'))).all()
        for run_id in run_ids:refresh_run(db,run_id)
        assert all(db.get(EvaluationRun,i).status=='completed' for i in run_ids),'replay_not_finished'
        assets=db.scalars(select(MaterialAsset)).all()
        unavailable=sum(not a.available or not Path(a.source_path).is_file() for a in assets)
    export_report()
    summary=json.loads((target/'summary.json').read_text(encoding='utf-8'))
    retest=json.loads((target/'targeted-retest.json').read_text(encoding='utf-8'))
    lines=['# 三板块开发与 DeepSeek 全量回放验收报告','',
        '## 结论',
        '本地演练闭环已建立：AI 被动回复、固定内容 SOP、沉默唤醒共用安全约束与隔离运行记录。模型结果仅为草稿，不能据此宣称业务准确率或转化提升。',
        '当前仍不应直接开启无人值守实发：报价、团期、证件与健康政策、其他线路内容仍缺少可核验资料；业务事实、原图适用性及语气需人工复核。','',
        '## 数据与测试范围',
        f"- 只读冻结：{summary['counts']['conversations']} 个会话，{summary['counts']['messages']} 条消息，Account 180474 / Inbox 128859。",
        '- 不再以桃花关键词筛选；遍历全部公开可用客户文本轮次。排除活动记录、私密/空内容、纯附件目标及测试触发轮次，连续客户文本合为一轮。',
        '- 上下文仅包含目标之前内容，最多最近 30 条；历史人工答案留作对照，不传入模型，不作为知识真值。',
        '- 唤醒在历史确认回复后 2 小时重建；该时点之前的新客户发言会排除旧候选。当时的标签/权限无法完整复原，按模拟条件明确区分。',
        '- SOP 在冻结客户上下文上使用标准化上海 10:00 虚拟时钟验证调度，不把历史营销参数当作最优策略。','',
        '## 汇总',
        '| 模块 | 案例 | 最终失败 | 最终结构有效 | P50 / P95 | 累计模型请求 |',
        '|---|---:|---:|---:|---|---:|']
    names={'reply':'AI 被动回复','wakeup':'沉默客户唤醒','sop':'SOP 固定内容'}
    for module in summary['modules']:
        m=module['metrics']
        lines.append(f"| {names[module['module']]} | {m['processed']} | {sum(m['failure_reasons'].values())} | {m['schema_valid_rate']:.1%} | {m['p50_ms']/1000:.2f}s / {m['p95_ms']/1000:.2f}s | {m['request_count']} |")
    lines+=['','耗时采用每个案例最后一次决策的全流程耗时，含该次重试；请求和 Token 数保留最初回放、失败重试及定向修复的全部开销。结构有效仅代表可解析，不代表事实正确。','', '## 模块结果']
    for module in summary['modules']:
        name=module['module'];m=module['metrics']
        cases=json.loads((target/f'{name}-cases.json').read_text(encoding='utf-8'))['cases']
        lines += [f"### {names[name]}",f"涉及 {len({x['conversation_id'] for x in cases})} 个有可用轮次的会话；数据快照覆盖 {module['snapshot']['conversation_count']} 个会话。",f"决策分布：{json.dumps(m['actions'],ensure_ascii=False)}。"]
        if name=='wakeup':lines += [f"唤醒分布：{json.dumps(m['wakeup_actions'],ensure_ascii=False)}。生成草稿不等于允许发送，仍需时段、窗口、频控和最新权限检查。"]
        if name=='sop':lines += [f"共 {m['scheduler_assertions_passed']} 项场景断言通过：到期执行、客户新回复取消、人工阻断、过期、渠道窗口、缺图、暂停、前序确认与共享频控。没有模型改写或实际网络发送。"]
        else:
            top=sorted(m['handoff_reasons'].items(),key=lambda x:x[1],reverse=True)[:5]
            lines += [f"转人工建议占比 {m['handoff_rate']:.1%}；主要原因：{json.dumps(dict(top),ensure_ascii=False)}。",f"问题意图（模型分类，未人工标注）：{json.dumps(m['intents'],ensure_ascii=False)}。",f"有事实引用的案例 {m['evidence_cited_cases']}；待复核 {m['review_pending']}。引用 ID 存在不能证明回复被完整支持。",f"累计输入 Token {m['input_tokens']:,}，输出 Token {m['output_tokens']:,}。请求状态：{json.dumps(m['request_outcomes'],ensure_ascii=False)}；错误：{json.dumps(m['request_errors'],ensure_ascii=False)}。"]
        lines += [f"排除明细（各项单位为消息或候选轮次，不可直接相加为会话数）：{json.dumps(module['snapshot']['exclusions'],ensure_ascii=False)}。",'']
    lines += ['## 本轮发现与修复',
        '- `no_action` 的空分支导致结构解析失败：现允许未确定线路的 `unclassified`，并仅重试原失败案例。',
        f"- 产品标题中的“六人小团”曾被误计为实际人数，以及少量无资料花期断言：增加客户原文证据校验与保护，共定向重测 {retest['affected_count']} 个受影响案例，未重新运行其余案例。清单见 `targeted-retest.json`。",
        '- “2~3位”不再被截为3人；不得从报价中“每人/一人”的描述推断同行人数。复杂人数表达仍需业务人工复核。',
        '- 人工建议草稿不再声称已经创建人工任务或转交客服；本轮实际人工任务零新增。',
        '- 修复重复 conversation_updated 被错误去重、消息时间误脱敏、移动端与聊天气泡样式冲突；补上 Inbox 范围、策略版本冲突及联系人并发预留校验。','',
        '## 安全与验证',
        '- 真实 outbox 仍为基线2条，人工任务仍为0条；配置与传输层阻断 Chatwoot 写请求。仅发送 DeepSeek 模型评测请求。',
        '- 空库升级、现有备份无损升级及 SQLite 完整性检查通过；详细计数见 `safety-and-migration.json`。',
        '- 回归测试、前端构建、桌面1440×1000和手机390×844浏览器检查通过；截图和检查结果保存在同一目录。',
        '- 未给未标注指标命名为业务准确率；未人工复核的案例继续标为待复核。风险词保护命中数也不是“实际幻觉数”。','',
        '## 上线前仍需完成',
        f'- 素材索引共 {len(assets)} 项，当前可访问路径 {len(assets)-unavailable} 项、不可用 {unavailable} 项。文件存在不等于原图内容或使用权限已验收；本轮已完成上传、绑定、预览和缺图阻断，测试图片只用于结构验证。',
        '- 补齐并由业务审核：价格与币种、团期余位、费用包含、单房差、儿童政策、合同退改、资格与健康相关表述、季节路线差异及公司联系方式。',
        '- 逐案例复核：是否回答问题、事实依据、分支与槽位、是否应转人工、语气和隐私。保守转人工可减少风险，但当前覆盖量不代表已经达到业务效果要求。',
        '- 真实标签同步时效、Facebook/IG实际送达、主动营销合法授权与实际转化不在本轮验收范围。需单独授权后做白名单实发验收。',
        '- 在持续服务、监控、密钥轮换和数据保留策略明确前，不把本地 SQLite/单 Worker 演练环境当生产部署。','',
        '## 文件索引',
        '- `summary.json`：机器可读汇总及快照哈希。',
        '- `reply-cases.json`、`wakeup-cases.json`、`sop-cases.json`：逐案例问题、上下文、参考、草稿与校验轨迹。',
        '- `targeted-retest.json`：修复影响范围。',
        '- `safety-and-migration.json`、`playwright-result.json`、`visual-result.json`：安全与界面验证。',
        '- `screenshots/`：桌面和移动端截图。',
        '- 以上文件含业务内容，只保存在本地，不属于公开 Demo。']
    (target/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({'report':str((target/'report.md').resolve()),'modules':[{k:x[k] for k in ['module','status']} for x in summary['modules']]}))


if __name__=='__main__':main()
