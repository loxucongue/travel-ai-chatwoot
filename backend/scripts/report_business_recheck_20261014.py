"""Build a requirement-by-requirement report from actual isolated test results."""
import hashlib
from html import escape
import json
import re
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'output/new-routes-20261014'


def main():
    deployment_path = OUT/'deployment-deploy.json'
    deployment = json.loads(deployment_path.read_text(encoding='utf8')) if deployment_path.exists() else None
    deployed = bool(deployment and deployment.get('deployed') and deployment.get('healthy'))
    verification = None
    if deployed:
        verification = json.loads((OUT/'deployment-verification.json').read_text(encoding='utf8'))
        assert verification['verified']
    root = ET.parse(OUT/'business-recheck/final-suite.xml').getroot()
    tests = list(root.iter('testcase'))
    failures = [t.get('name') for t in tests if t.find('failure') is not None or t.find('error') is not None]
    assert tests and not failures, failures
    suites = list(root.iter('testsuite'))
    passed = sum(int(s.get('tests', 0))-int(s.get('failures', 0))-int(s.get('errors', 0))-int(s.get('skipped', 0)) for s in suites)
    stage = json.loads((OUT/'staging-seasonal-retention/summary.json').read_text(encoding='utf8'))
    prices = json.loads((ROOT/'data/knowledge/china2go/route-releases/20261014/price-sheet-reconciliation.json').read_text(encoding='utf8'))
    assert stage['assets_resolved'] == 32 and stage['production_changed'] is False
    ledger = json.loads((ROOT/'output/acceptance-20261013/cost.json').read_text(encoding='utf8'))
    known_cost = sum(c['charged_or_reserved_cny'] for c in ledger['calls'] if c.get('usage_known'))
    unknown_reserve = sum(c['charged_or_reserved_cny'] for c in ledger['calls'] if not c.get('usage_known'))
    models = json.loads((OUT/'business-recheck/model/summary.json').read_text(encoding='utf8'))
    blocked = [m['case'] for m in models if m['failures']]
    preflight = json.loads((OUT/'business-recheck/model-budget-preflight.json').read_text(encoding='utf8'))
    def shell_log(name):
        raw = (OUT/name).read_bytes()
        return raw.decode('utf-16' if raw.startswith(b'\xff\xfe') else 'utf-8-sig')
    frontend = shell_log('business-frontend-tests.txt')
    frontend_passed = int(re.search(r'# pass (\d+)', frontend)[1])
    assert re.search(r'# fail 0\b', frontend)
    assert 'built in' in shell_log('business-frontend-build.txt')
    runtime_tests = [t for t in tests if 'test_seasonal_runtime_journeys' in t.get('classname', '')]
    assert len(runtime_tests) == 16
    rows = [
        ('01', '客户选线后必须有开场，后加 ai 标签也不能漏接', '新鲜未答消息可补启动；旧消息、已答、人工占有不重放。', ['model_recognized_route_wait', 'ai_added_after_customer_message'], '自动化通过'),
        ('02', '人数不答等待2分钟，有回答立即往下', '桃花线120秒；新增8/10日按网页问月份、30秒。', ['intake_actual_delivery', 'any_ordinary_answer', 'new_route_intake'], '自动化通过；两种开场隔离'),
        ('03', '不要多问已知人数，介绍中问题不能丢', '已知人数跳过开场；介绍中的问题保存，完成介绍后回答。', ['first_message_known_party', 'ordinary_answer', 'buffer'], '自动化通过；自然表达保留模型历史证据'),
        ('04', '未问价格不主动报价，不展开额外差价', '介绍和跟随不报价；普通问价使用本线路整段模板。最新要求保留“包含/不含”。', ['price_presentation', 'restore_quote_template', 'effect_checker_catches_new_route_prices'], '配置/自动化通过；当前模型扩展复测受预算限制'),
        ('05', '报价结束必须有轻松问题', '关系未知问家人还是朋友；已知关系不重复追问，独行不硬问同行。', ['quote_engagement', 'restore_quote_template'], '模板回归通过；#625式真实模型复测受预算限制'),
        ('06', '不要一介绍完就索取LINE', '结束介绍轻松了解同行；沉默节点不催联系方式。', ['family_wording_upgrade', 'invitation_scenario_upgrade', 'route_invitation_upgrade'], '配置回归通过；自然表达沿用历史模型结果'),
        ('07', '把LINE ID改成LINE QR Code', '邀请用QR Code；客户主动给有效ID或Email仍可接收，不强迫再传图。', ['contact_script_migration', 'contact_capture_refusal', 'line_qr'], '自动化通过；渠道语义仍需模型验收'),
        ('08', '首次报价起30天，同一客户固定', '按客户和线路保存截止日；重复报价不续期，过期使用标准价。新常规线使用季节表。', ['followup_offer_20261013', 'historical_migration_cannot_downgrade'], '自动化通过；补防旧迁移覆盖新优惠'),
        ('09', '首小时10、25、40、60分钟触达', '以首次进入为锚点；延迟任务跳过错过节点，不集中补发。', ['full_cadence', 'entry_anchor', 'actual_night_first_hour'], '自动化通过'),
        ('10', '后续每3小时；1点到7点跳过，首小时保留', '只跳过后续夜间节点；客户回复取消旧计划，答复完成后重新按3小时计时。', ['customer_reply_uses_three_hours', 'delivery_crossing_one_am', 'new_incoming_cancels'], '自动化通过；遵守渠道窗口，不无限发送'),
        ('11', '沉默跟随要有照片，不能第一波介绍后就没了', '未发送图片按哈希去重，每节点一张加说明；新路线32处图片引用可实际解析。', ['followup_photos', 'sent_photo_hash'], '图片交付自动化及本地素材演练通过'),
        ('12', '模型不能随意取消后续唤醒', '普通wait或stop_followup不取消整套计划；拒绝、人工、明确预约等才暂停。', ['six_hour_timeline', 'multiple_configured_checkpoints', 'last_configured_round'], '自动化通过；旧6小时测试仅验证可配置机制'),
        ('13', '客户回来、换线、人工介入时不发过时消息', '取消旧草稿和跟随；发送前复查权限，未知送达不盲目重发。', ['delivery_conflicts', 'new_incoming_cancels', 'unknown_delivery'], '自动化通过'),
        ('14', '人工标签与ai标签的影响范围要明确', '人工/拒绝等阻断优先；明确恢复AI需完成任务，不能覆盖同客其他人工会话。', ['label_handoff_transitions', 'conversation_controls'], '自动化通过'),
        ('15', '所有转人工当轮也不要发给客户消息', '文字、图片、已排队介绍全部停止；只建内部接管任务和通知。', ['silent_handoff'], '自动化通过'),
        ('16', '顾问接收通知，指定事件均分给顾问', '内部通知、均分、优先级、重复事件去重，保留已有人工负责人。', ['advisor_assignment', 'handoff_to_private_notification'], '自动化通过；实际线上通知未发送测试'),
        ('17', '人数/天数/留资/无法回答等正确转人工', '新增常规线7人以上硬约束；其他事件遵循既有规则；交接均静默。', ['new_route_seven_people', 'profile_rule_stops', 'self_reported_contact', 'live_handoff_is_silent'], '自动化通过；原全局人数阈值8人未擅自改写'),
        ('18', '用“我们可以协助”，保持接待口吻一致', '普通答疑第一人称；实际交接不发过渡语，不冒称某位真人。', ['silent_handoff', 'contact_script_migration'], '配置/静默控制通过；当前语言复测受预算限制'),
        ('19', '客户改期/后年再去，不要直接结束销售', '可说明西藏其他季节和中国其他目的地，适时邀请联系；不编未来团期。', ['route_invitation_upgrade', 'invitation_scenario_upgrade'], '配置回归通过；本轮后年场景模型复测受预算限制'),
        ('20', '拒绝留LINE不等于拒绝所有服务', '保留渠道偏好，明确拒绝联系才停；已留资不反复索取。', ['contact_capture_refusal', 'self_reported_contact', 'optout'], '自动化通过'),
        ('21', '文字点赞/表情继续接待', '现有文本处理无需看图；需验证模型不误判交接、留资或人数。', [], '本轮真实模型测试被预算拦截，未算通过'),
        ('22', '图片点赞不误转人工', '当前没有图片语义分类；作为图片附件的点赞仍可能进入人工核对。', [], '未完成：需要图片分类能力及真实附件验证'),
        ('23', '普通图片提醒人工；二维码不能虚称识别成功', '未知图片/待核二维码只做内部交接，不对客说“收到图片”“已加好友”。', ['silent_handoff', 'line_qr'], '静默控制通过；本轮图片语义模型复测受预算限制'),
        ('24', '新增8/10日价格按日期和线路匹配', '7套季节表已逐格核对Excel；未开放日期不报价，长假和不足30天仍需顾问确认；季节未开放按第27项承接。重新查日期失败，前一次报价授权失效。', ['departure_date_price_boundaries', 'notice_30_days', 'free_text_price', 'workbook_', 'price_sheet_disagreement', 'eight_day_product', 'test_seasonal_runtime_journeys'], '自动化通过；补测四线路2027全年1460个日期组合及实际工具循环'),
        ('25', '新增线路配置能导入、再编辑、保存', '真实API导入和修改后保留线路开场与价格规则；原线路和全局策略不被覆盖。', ['new_route_import_and_edit'], '自动化通过；本地四线路实库配置通过'),
        ('26', '不能把所有珠峰需求都带到桃花11日', '含糊珠峰需求先澄清；明确8天含珠峰不强行切到不上珠峰9日。', ['new_route_import_and_edit', 'sent_photo_hash'], '路由配置回归通过；此前真实模型场景通过'),
        ('27', '季节未开放先推荐同期产品，或登记下次开放通知', '四线路提供日期核对；只返回同期已启用团期，不自动切线。无合适选择时轻邀一种联系方式；停止原介绍/报价，留资后静默交接顾问跟进。拒绝、不留资、已有联系方式等约束仍有效。', ['test_seasonal_retention', 'seasonal_runtime_repair', 'seasonal_runtime_failed_repair'], '工具核对→拦截错误决策→取消旧图文的集成测试通过；邀请语气/偏好识别仍待真实模型复测'),
    ]
    records = []
    for code, requirement, implementation, patterns, verdict in rows:
        matched = sorted({f"{t.get('classname')}::{t.get('name')}" for t in tests
                          if any(p in (t.get('classname', '')+'::'+t.get('name', '')) for p in patterns)})
        assert not patterns or matched, code
        records.append({'id':code,'requirement':requirement,'implementation':implementation,
                        'verdict':verdict,'automated_evidence':matched})
    missing = [
        '已核对补交的2027-售價表總表-1002.xlsx：新增两线7档金额/单房差与网页一致；已保存原件、哈希、单元格出处及对照结果。早前误发的账号导出未使用。',
        '业务最新澄清：8日4/21–4/24等季节性未开放时段不再当作缺价直接交接，先推荐同期产品或邀请开放通知。长假特殊报价仍缺，其他年份团期尚未公布。10日春季虽列11,980元但不开放自动销售。',
        '冰川冬游8日价格已从表内确认9,280元/单房差1,500元；它是另一产品，完整资料/素材仍未提供，本次不自动销售。',
        '表内A41允许按当地安排调整顺序但不减少景点，8日第2/3天先后不作为价格缺失阻塞；10日林芝酒店具体分店仍待确认。',
        '表内早鸟与多人优惠允许叠加，但计价单位、月份边界及与已批准活动价的叠加关系未明确，不自动计算折后金额。桃花11日12,180活动价及首次报价30天规则保留。',
        '图片点赞分类属于尚未完成的能力，不是价格资料问题。',
    ]
    summary = {'scope':'Latest business requirements plus two new routes', 'business_acceptance_complete':False,
               'production_changed':deployed, 'deployment':deployment, 'deployment_verification':verification,
               'backend_passed':passed,'backend_failed':0,
               'frontend_passed':frontend_passed,'frontend_build':'passed','frontend_rerun_this_round':True,'assets_resolved':32,'unique_images':20,
               'additional_runtime_integration_tests':len(runtime_tests), 'full_calendar_route_date_combinations':1460,
               'runtime_model_responses_simulated':True, 'model_budget_preflight':preflight,
               'historical_model_cases_passed':11,'historical_model_checkpoints_passed':31,
               'new_model_cases_blocked':blocked,'new_model_cost_cny':round(ledger['charged_or_reserved_cny']-19.32446488,8),
               'ledger_total_cny':ledger['charged_or_reserved_cny'],'requirements':records,'missing_or_limited':missing,
               'ledger_known_cost_cny':round(known_cost,8),'ledger_unconfirmed_reserve_cny':round(unknown_reserve,8),
               'price_sheet_reconciliation': prices, 'staging_evidence': 'staging-seasonal-retention/summary.json',
               'seasonal_retention_model_cases_pending': ['new8_date_gap','winter_is_not_regular_eight','closed_season_contact_then_silent_handoff','closed_season_respects_contact_refusal','closed_ten_day_offers_same_period_options'],
               'pending':['图片点赞语义分类与真实附件验证','原4个预算阻塞的模型回放及本次季节承接5个场景复测',
                          '新线路真实客户对话与渠道实际图文送达效果待观察；上线健康和素材就绪检查已通过' if deployed else '实际部署及线上渠道验证',
                          '开放通知由顾问跟进，尚无自动监测产品重新开放的服务'],
               'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in [ROOT/'backend/app/reception_v3/runtime.py', ROOT/'backend/app/reception_v3/skills/tibet-reception/SKILL.md', ROOT/'backend/tests/test_seasonal_runtime_journeys.py', ROOT/'backend/app/reception_v3/seasonal_quotes.py', ROOT/'backend/scripts/update_business_feedback_20261011.py', ROOT/'backend/scripts/evaluate_reception_journeys.py', ROOT/'backend/scripts/reconcile_tibet_prices_20261014.py', ROOT/'data/knowledge/china2go/price-sheet-20261014/cells.json']}}
    (OUT/'business-recheck/summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    css='body{max-width:1200px;margin:auto;padding:28px;font:16px/1.7 system-ui;background:#f5f7fa;color:#193346}h1{font-size:28px}table{border-collapse:collapse;width:100%;background:white}th,td{border:1px solid #d4dce3;padding:12px;text-align:left;vertical-align:top}th{background:#eaf0f5}.note{padding:18px;background:#fff1cc;border-left:4px solid #c49321}details{font-size:13px}a{color:#116c9c}pre{white-space:pre-wrap;overflow-wrap:anywhere}.stats{padding:14px;background:#e1f1e8}li{margin:8px 0}'
    status_text = ('代码与四线路配置已部署，发布后健康和素材检查通过；整体业务效果仍有以下未完成项。'
                   if deployed else '自动化及配置验收通过；整体业务效果验收尚未全部完成，未部署。')
    deployment_link = '<p><a href="tibet-routes-production-20261014.html">查看上线结果、复核证据和回滚记录</a></p>' if deployed else ''
    doc=[f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>业务回复效果全量复核 · 2026-10-14</title><style>{css}</style>',
         '<h1>业务回复效果全量复核 · 2026-10-14</h1>',
         f'<p class="note"><strong>{status_text}</strong>图片点赞分类仍未实现；原4个模型回放受预算限制，本次5个季节承接场景尚待真实模型复测。历史11个模型场景中“缺价/冬季直接交接”的2项已被最新业务口径取代，不能作为当前通过证据。</p>'+deployment_link,
         f'<p class="stats">后端 {passed} 项通过 / 0 失败 · 前端4项通过及构建成功 · 32处素材引用 / 20张原图 · 本轮新增模型费用 ¥{summary["new_model_cost_cny"]:.2f}</p>',
         '<h2>这轮发现并修复</h2><ol><li>旧报价迁移会混用固定截止日与30天有效期：保留新版滚动报价，修正固定日期测试前提，增加防回退测试。</li><li>普通数字/RMB写法可绕过季节核价：团费自由报价统一拦截，仍允许回答小费、单房差等独立问题。</li><li>“不主动报价”的效果检查器仅识别桃花价格：现在覆盖所有已配置季节价格，防止漏判新线路。</li><li>补测两条线路真实导入、编辑、保存与运行时读取；30秒月份开场及价格表保留，原全局配置不受影响。</li></ol>',
         '<h2>本次继续测试：新增发现与验证</h2><p>新增16项集成/日历测试，其中逐日检查四线路2027全年共1460个线路日期组合，同时反查每条推荐线路在该日确实开放。前端4项测试与构建已重新执行。</p><ul><li>复现并修复：先查到有效报价后，再查日期若缺参数、JSON损坏或参数为null，旧报价授权可能继续被使用。现在失败即撤销相应授权，禁止复用旧价格；保留初次失败及修复后记录。</li><li>再次扩展报价格式：仅写人民币¥7,980或“这条行程只要7980元”也不能绕过核价；独立房差/机票/小费、已知赔偿和客户预算不混为团费，并补测其边界。</li><li>验证真实运行时工具循环：查团期→发现错误介绍→要求修正→取消旧文字/图片→保留当前线路；连续错误不会返回可发送决策。</li><li>验证核价授权不跨线路、改为长假/未开放/错误日期后旧价格失效；完整报价原文及末尾问题能进入实际交付层。</li><li>清理通用提示中“当前只有两条桃花线”的过时描述，按当前启用目录判断。</li></ul><p>这16项测试采用模拟模型响应，验证代码约束和交付衔接，不能替代真实模型对客户语言的理解验收。<a href="../../output/new-routes-20261014/business-recheck/runtime-first.txt">工具参数修复前失败复现</a> · <a href="../../output/new-routes-20261014/business-recheck/runtime-fixed.txt">相关60项修复后回归</a> · <a href="../../output/new-routes-20261014/business-recheck/price-format-first.txt">报价写法补测失败复现</a> · <a href="../../output/new-routes-20261014/business-recheck/price-format-fixed.txt">报价格式修复后回归</a></p>',
         '<h2>资料核对与边界</h2><ul>'+''.join('<li>'+escape(x)+'</li>' for x in missing)+'</ul>',
         '<p><a href="../../data/knowledge/china2go/price-sheet-20261014/2027-售價表總表-1002.xlsx">报价表原件（未修改）</a> · <a href="../../data/knowledge/china2go/route-releases/20261014/price-sheet-reconciliation.json">逐档核对及规则取舍</a>。本轮没有改动价格数字；配置新增出处，重建隔离四线路环境验证导入。</p>',
         '<table><tr><th>新增线路</th><th>Excel西藏页单元格 / 名义季节</th><th>团费 / 单房差（人民币）</th><th>核对</th></tr>'+''.join(f'<tr><td>{"精华8日" if b["route_variant"]=="tibet_essence_8d" else "全览10日"}</td><td>{escape(b["cells"])} / {escape(b["nominal_period"])}</td><td>{b["price_cny"]:,} / {b["single_supplement_cny"]:,}</td><td>与配置一致；长假限制优先</td></tr>' for b in prices['new_route_bands'])+'</table>',
         '<h2>27项业务要求与对应验收</h2><p>采用最近确认口径。旧版6小时、单句简价、不写不含、季节未开放直接交接等已被后续要求覆盖。表中的“自动化”验证日期、状态、配置和交付，不冒充真实模型语言效果验收。</p><table><tr><th>编号 / 业务要求</th><th>当前实现</th><th>验收结论 / 证据</th></tr>']
    for r in records:
        evidence='<details><summary>自动化测试证据</summary><pre>'+escape('\n'.join(r['automated_evidence']) or '无当前自动化语义证据')+'</pre></details>'
        doc.append(f'<tr><td>{r["id"]} · {escape(r["requirement"])}</td><td>{escape(r["implementation"])}</td><td>{escape(r["verdict"])}{evidence}</td></tr>')
    doc.extend(['</table><h2>测试范围与记录</h2>',
        '<p>上述隔离测试使用临时/内存数据库，真实外部网络连接被禁止；本地事件循环的回环连接允许。上一轮4项模型回放在预算预留阶段被阻止，本轮仅离线预检真实请求大小，未发起付费调用。隔离测试未写生产配置，没有向客户发送测试消息。前端4项测试与构建本轮重新执行通过。后续实际部署及其复核单独记录。</p>',
        '<p>第一次全量回归400通过、2失败；修复后运行全量回归，结果见下方日志。另保留报价绕过的5个失败复现用例。最初测试网络隔离误拦Windows内部回环，测试框架异常退出，该次未计入通过结果。</p>',
        '<ul><li><a href="../../output/new-routes-20261014/business-full-tests-final.txt">最终全量后端日志</a> / <a href="../../output/new-routes-20261014/business-recheck/final-suite.xml">逐项JUnit结果</a></li><li><a href="../../output/new-routes-20261014/business-frontend-tests.txt">前端测试</a> / <a href="../../output/new-routes-20261014/business-frontend-build.txt">构建</a></li><li><a href="../../output/new-routes-20261014/staging-seasonal-retention/summary.json">季节承接修改后四线路本地配置与真实素材演练</a></li><li><a href="../../output/new-routes-20261014/business-recheck/model/summary.json">4项模型复测阻塞记录</a></li><li><a href="new-tibet-routes-20261014.html">此前线路验收与11个历史模型场景</a></li><li><a href="../../output/new-routes-20261014/business-recheck/summary.json">完整业务验收矩阵JSON</a></li></ul>',
        f'<p>模型原20元账本累计计费/保留 ¥{ledger["charged_or_reserved_cny"]:.4f}（已知用量折算¥{known_cost:.4f}，历史未确认请求保留¥{unknown_reserve:.4f}），剩余约¥{20-ledger["charged_or_reserved_cny"]:.4f}；本轮离线预检4个完整请求均需预留约¥0.72–0.74，未调用模型、未改动账本。<a href="../../output/new-routes-20261014/business-recheck/model-budget-preflight.json">预算预检证据</a>。没有清零历史账本或借用附件账号。</p>',
        '<h2>尚未完成</h2><ul>'+''.join('<li>'+escape(x)+'</li>' for x in summary['pending'])+'</ul></html>'])
    target=ROOT/'docs/development/business-recheck-20261014.html'
    target.write_text('\n'.join(doc),encoding='utf8')
    print(json.dumps({k:summary[k] for k in ['backend_passed','backend_failed','business_acceptance_complete','new_model_cost_cny','new_model_cases_blocked']}))


if __name__ == '__main__':
    main()
