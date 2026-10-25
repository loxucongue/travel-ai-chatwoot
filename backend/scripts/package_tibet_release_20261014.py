"""Package reviewed code/data/media and publish the local acceptance report."""
import hashlib
from html import escape as esc
import json
from pathlib import Path
import re
import zipfile

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'output/new-routes-20261014'
RELEASE=ROOT/'data/knowledge/china2go/route-releases/20261014'


def main():
    deployed_path = OUT/'deployment-deploy.json'
    if deployed_path.exists() and json.loads(deployed_path.read_text(encoding='utf8')).get('deployed'):
        raise SystemExit('This release is already deployed; preserve its archive and manifest. Use a new release directory for a new package.')
    source_cases={
        'new10_full_journey':'model-tests-v3','new8_quote':'model-tests-v2','peach_regression':'model-tests-v2',
        'new10_seven_people':'model-tests-v1','new8_date_gap':'model-tests-v1','new10_short_notice':'model-tests-v1',
        'new10_holiday':'model-tests-v1','everest_ambiguous':'model-tests-v1',
        'new8_unknown_departure_quote':'model-guards-v2','winter_is_not_regular_eight':'model-guards',
        'eight_spring_keeps_everest':'model-guards'}
    cases=[]
    for key,folder in source_cases.items():
        result=json.loads((OUT/folder/(key+'.json')).read_text(encoding='utf8'))
        assert result['steps'] and all(not s['failures'] for s in result['steps']),key
        cases.append({'id':key,'result_file':f'{folder}/{key}.json','steps':len(result['steps']), 'result':result})
    latest_path = OUT/'business-recheck/summary.json'
    latest = json.loads(latest_path.read_text(encoding='utf8')) if latest_path.exists() else None
    log_path = OUT/('business-full-tests-final.txt' if latest else 'final-tests.txt')
    log=log_path.read_text(encoding='utf-16')
    passed=int(re.search(r'(\d+) passed',log)[1])
    assert passed >= 183 and not re.search(r'\d+ (failed|error)',log)
    staging_folder = 'staging-seasonal-retention' if latest else 'staging-v3'
    staging=json.loads((OUT/staging_folder/'summary.json').read_text(encoding='utf8'))
    assert staging['assets_resolved']==32 and staging['unique_images']==20
    ledger=json.loads((ROOT/'output/acceptance-20261013/cost.json').read_text(encoding='utf8'))
    cost=round(ledger['charged_or_reserved_cny']-18.27496552,8)
    assert ledger['charged_or_reserved_cny'] <= 20
    configuration={'add_enabled_route_variants':['tibet_essence_8d','tibet_overview_10d'],
        'preserve_existing_routes':True,'preserve_existing_config_sections':['reply','intake','silence','handoff','lead_capture','common_scripts'],
        'route_intake_override':{'field':'departure_window','wait_seconds':30},
        'existing_route_availability_patch': 'configure_seasonal_retention_20261014.update_package; preserve all other runtime fields',
        'deployment_state':'prepared_not_applied'}
    (RELEASE/'configuration-patch.json').write_text(json.dumps(configuration,ensure_ascii=False,indent=2),encoding='utf8')
    runbook={'release':'tibet-routes-20261014','scope':'Add two routes; preserve existing peach products and global timing',
        'prepared_only':True, 'outbound_tested':False,
        'latest_business_acceptance_complete':latest['business_acceptance_complete'] if latest else None,
        'install_steps':[
            'Read and verify current production configuration, code and media readiness against the saved baseline; abort on unexplained drift.',
            'Back up deployed code, runtime route packages, database and route_reception_config; record backup path and hashes.',
            'Pause background AI workers while applying the package; preserve incoming webhooks for later processing.',
            'Install the reviewed runtime files listed by manifest; install the verified original media into tenant-scoped StoredMedia and MaterialAsset records. Use fresh local IDs, never staging IDs.',
            'Use the existing validated route import path to install the two route-package.json files, preserving every other route/config section.',
            'Apply configure_seasonal_retention_20261014.update_package to backed-up current peach runtime packages then validate/import; only departure_availability is added, preserve prices and operator changes.',
            'Check all 32 hashes, tenant/route bindings and both new route Skills before enabling new traffic.',
            'Restart API/worker services; verify health and readiness. Run isolated channel smoke tests before reopening advertising.',
            'No historical customer replay, no public test messages, no automatic advertising changes.'],
        'rollback_steps':[
            'Pause workers; stop new-route automatic reception and cancel only unsent new-route test/delivery plans.',
            'Restore old enabled-route list and runtime packages plus code from the release backup.',
            'Keep message/history and media evidence. Do not wholesale restore an old database over new customer messages.',
            'Restart services; verify original two routes, silent handoff and followup readiness.'],
        'limits':['Only 2027 seasonal price table is configured.',
            'Seasonal closures or unpublished dates offer dated alternatives or opt-in reopening notification; no fifth route activated. Actual contact is handed to an advisor.',
            'No automated product-reopening monitor exists; reopening notification registration is an internal advisor task.',
            'Workbook A41 permits itinerary order adjustments without removing sights. The 10-day Nyingchi hotel branch still needs confirmation.',
            'Workbook amounts verified; nominal seasons do not override approved holiday exclusions or unsold dates. No automatic discount stacking.',
            'Unverified oxygen figures, mobile oxygen cabin claims and stale vehicle model-year superlatives are excluded.',
            'Global silence schedule and silent handoff override conflicting old webpage instructions.'],
        'local_rehearsal':staging}
    (OUT/'deployment-plan.json').write_text(json.dumps(runbook,ensure_ascii=False,indent=2),encoding='utf8')
    files=[ROOT/p for p in [
        'backend/app/route_packages.py','backend/app/reception_v3/service.py','backend/app/reception_v3/runtime.py',
        'backend/app/reception_v3/skills.py','backend/app/reception_v3/seasonal_quotes.py',
        'backend/app/reception_v3/skills/tibet-reception/SKILL.md','backend/app/reception_v3/skills/peach-11d-2027/SKILL.md',
        'backend/app/reception_v3/skills/tibet-essence-8d/SKILL.md','backend/app/reception_v3/skills/tibet-overview-10d/SKILL.md',
        'backend/scripts/stage_tibet_routes_20261014.py','backend/scripts/prepare_tibet_routes_20261014.py',
        'backend/scripts/update_business_feedback_20261011.py', 'backend/scripts/evaluate_reception_journeys.py',
        'backend/scripts/report_business_recheck_20261014.py',
        'backend/scripts/reconcile_tibet_prices_20261014.py', 'backend/tests/test_tibet_workbook_reconciliation.py',
        'backend/scripts/configure_seasonal_retention_20261014.py', 'backend/tests/test_seasonal_retention.py',
        'backend/tests/test_seasonal_runtime_journeys.py',
        'backend/tests/fixtures/seasonal_retention_journeys_20261014.json',
        'backend/tests/test_tibet_release_20261014.py', 'backend/tests/test_tibet_configuration_acceptance.py',
        'backend/tests/fixtures/recent_business_regressions_20261014.json']]
    files += list(RELEASE.rglob('*.json'))+list((ROOT/'data/knowledge/china2go/website-1013-20261014').glob('*'))
    files += list((ROOT/'data/knowledge/china2go/price-sheet-20261014').glob('*'))
    entries={str(p.relative_to(ROOT)).replace('\\','/'):p for p in files}
    for file in (OUT/staging_folder/'media').iterdir(): entries['release-media/'+file.name]=file
    entries['deployment-plan.json']=OUT/'deployment-plan.json'
    if latest:
        entries['business-recheck-summary.json']=latest_path
        entries['business-full-tests-final.txt']=log_path
        entries['business-final-suite.xml']=OUT/'business-recheck/final-suite.xml'
        entries['model-budget-preflight.json']=OUT/'business-recheck/model-budget-preflight.json'
    manifest={key:{'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size} for key,path in entries.items()}
    archive=OUT/'tibet-routes-20261014-ready.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for key,path in entries.items(): z.write(path,key)
        z.writestr('manifest.json',json.dumps(manifest,indent=2))
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for key,record in manifest.items(): assert hashlib.sha256(z.read(key)).hexdigest()==record['sha256']
    summary={'status':'predeployment_prepared','production_changed':False,'unit_regression_passed':passed,
             'latest_business_acceptance_complete':latest['business_acceptance_complete'] if latest else None,
             'model_cases_passed':len(cases),'model_steps_passed':sum(c['steps'] for c in cases),
             'model_evidence_status':'historical_not_current_acceptance',
             'latest_round_model_cost_cny':latest['new_model_cost_cny'] if latest else cost,
             'superseded_model_case_ids':['new8_date_gap','winter_is_not_regular_eight'],
             'model_cases':[{k:v for k,v in c.items() if k!='result'} for c in cases],
             'new_model_cost_cny':cost,'budget_ledger_cny':ledger['charged_or_reserved_cny'],
             'assets':32,'unique_images':20,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    (OUT/'acceptance-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    css='''body{font:16px/1.75 system-ui,sans-serif;margin:auto;max-width:1120px;padding:32px;background:#f4f7fa;color:#183248}h1{font-size:30px}h2{margin-top:32px}table{border-collapse:collapse;width:100%;background:#fff}td,th{border:1px solid #d5dfe7;padding:12px;text-align:left;vertical-align:top}.ok{background:#e2f1e9;border-left:4px solid #20865e;padding:18px}.note{background:#fff1cc;border-left:4px solid #b78020;padding:18px}.stats{display:flex;gap:18px;flex-wrap:wrap}.stats div{background:white;padding:14px 24px;border:1px solid #d5dfe7}.stats b{font-size:27px;display:block}details{background:white;margin:12px 0;padding:14px}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 system-ui}a{color:#126a93}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}img{width:100%;height:180px;object-fit:contain}figure{margin:0}figcaption{font-size:12px;overflow-wrap:anywhere}'''
    html=[f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>两条西藏新线路 · 上线前验收</title><style>{css}</style>',
        '<h1>两条西藏新线路 · 上线前验收</h1><p>2026-10-14 · 西藏精華8天 / 西藏全覽10天</p>',
        ('<p class="note"><strong>最新全量复核：自动化通过，整体业务效果尚未全部验收。</strong>图片点赞分类未实现，原4个模型场景受预算限制，本次5个季节承接场景待真实模型复测。下方11个历史模型记录中“缺价/冬季直接交接”2项已被新口径替代。<a href="business-recheck-20261014.html">查看27项业务要求和最新验收结果</a>。</p>' if latest else ''),
        '<p class="ok"><strong>两条新线路的代码、配置和隔离测试已准备；整体上线验收仍有未完成项，尚未部署。</strong>浏览器已使用提供的密码读取<a href="https://china2go.com/1013-2/">业务原页</a>。补交报价表已核对7档金额/单房差，全部一致；原网页、Excel、ZIP映射和校验值已归档。<a href="business-recheck-20261014.html">查看最新报价表核对及业务验收</a>。线上配置与客户会话未修改。</p>',
        f'<div class="stats"><div><b>{passed}</b>自动化测试通过</div><div><b>{len(cases)} / {summary["model_steps_passed"]}</b>历史模型场景 / 节点（2项口径已更新）</div><div><b>32 / 20</b>素材引用 / 不同图片</div><div><b>¥{cost:.2f}</b>此前线路模型测试消耗，本轮新增¥{summary["latest_round_model_cost_cny"]:.2f}</div></div>',
        '<h2>现在的接待规则</h2><table><tr><th>环节</th><th>实现及验收</th></tr>',
        '<tr><td>选线</td><td>保留原桃花9/11日，增加常规8/10日。仅说珠峰先确认偏好，避免直接进入11日；8日拉萨进出，10日林芝进、拉萨出。</td></tr>',
        '<tr><td>开场与介绍</td><td>新线先问月份，30秒不答开始介绍；客户回复后先核对团期，可用时继续介绍，未开放时转为推荐/开放通知承接；介绍后才问未知人数。原桃花线路保持人数等待120秒。中途问题保留，介绍后回答。</td></tr>',
        '<tr><td>报价</td><td>客户询价才报，先核对日期；7套季节模板原样引用，包含单房差及含/不含。新线不套桃花优惠价或30天截止日。季节未开放先推荐同期产品或邀请开放通知；不足30天、长假特殊报价仍交顾问。</td></tr>',
        '<tr><td>收尾与沉默</td><td>报价后轻松问家人/朋友；已知关系不重复问。沉默继续10/25/40/60分钟及后续3小时，01–07点跳过后续节点，首小时保留。每个节点一张未发送照片加一句说明，不催联系方式。</td></tr>',
        '<tr><td>人工接管</td><td>新线7人以上、包团、只能长假须特殊报价、不足30天等静默交接。季节未开放不直接交接，客户实际留资后顾问收到内部开放通知登记任务。当轮不向客户发消息。</td></tr></table>',
        '<h2>2027年季节报价配置</h2><table><tr><th>线路</th><th>区间 / 每周团期</th><th>团费 / 人</th><th>单房差</th></tr>']
    packages=[json.loads(p.read_text(encoding='utf8')) for p in RELEASE.glob('*/route-package.json')]
    for p in packages:
        for s in p['fixed_answers']:
            if s['id'].startswith('price_'):
                lines=s['answer_text'].splitlines()
                html.append(f'<tr><td>{esc(p["name"])}</td><td>{esc(lines[1])}</td><td>{esc(lines[2])}</td><td>{esc(lines[3])}</td></tr>')
    html.append('</table><p class="note">最新业务澄清：未开放时段先推荐同期已启用产品，无合适选择可邀请登记开放通知。冬游未启用，不能套用常规8日；10日3/11–4/30不售；8日4/21–4/24未开放。2/1–2/16、4/25–5/5、9/25–10/14涉及长假时仍不套普通价；本线明确未开放的日期优先走季节承接。报价限2027年，不承诺其他年份团期。</p>')
    html.append('<h2>发现的问题与修正结果</h2><table><tr><th>问题</th><th>修正与结论</th></tr><tr><td>旧说明只有两条桃花线，珠峰别名直达11日</td><td>启用新线时移除含糊入口别名，并明确各线路适用范围；珠峰模糊咨询与8天需求实测通过。</td></tr><tr><td>开场等待时长全局共用</td><td>增加线路级开场字段与等待配置，原120秒不变，新线路30秒；到期与回复立即介绍均通过。</td></tr><tr><td>介绍后漏问人数</td><td>区分桃花/常规线收尾，常规线未知人数先问人数；复测通过。</td></tr><tr><td>一个沉默节点发两组图片</td><td>明确每节点最多一张照片，按哈希排除旧图；复测通过。</td></tr><tr><td>询价时日期问题后又追加说明</td><td>改为明确问句结尾，未知日期先问而不报低价；复测通过。</td></tr><tr><td>Windows测试日志遇¥与表情中断</td><td>输出改UTF-8，受影响场景重跑通过；保留初次失败记录，未冒充一次通过。</td></tr></table>')
    labels={'new10_full_journey':'10日完整接待、报价及沉默照片','new8_quote':'8日月份回复立即介绍与旺季报价','peach_regression':'原桃花11日等待、完整优惠报价及问句',
        'new10_seven_people':'7人静默转人工','new8_date_gap':'8日缺价日期不乱报价','new10_short_notice':'不足30天不报价','new10_holiday':'客户只能长假出发',
        'everest_ambiguous':'只说珠峰不强制11日，明确8天后正确选线','new8_unknown_departure_quote':'未知日期先询问，不报最低价',
        'winter_is_not_regular_eight':'冬游不冒充常规线','eight_spring_keeps_everest':'春季坚持8天含珠峰，不强制切9日'}
    html.append('<h2>逐项真实模型验收记录</h2><p>共11个隔离场景、31个检查节点；仅模型接口允许联网，客户消息发送已禁用。此前失败经定点修正及复测关闭，以下展示各场景最近一次有效结果。素材读取与四线路配置导入另在本地数据库演练通过；尚未做生产渠道发信验收。</p>')
    for case in cases:
        status = '历史结果，最新口径已替代' if case['id'] in summary['superseded_model_case_ids'] else '历史通过，变更后待复测'
        html.append(f'<details><summary>{status} · {esc(labels[case["id"]])} · {case["steps"]}个节点</summary>')
        for step in case['result']['steps']:
            html.append(f'<p><strong>{esc(step["id"])}</strong></p><pre>{esc(step.get("text") or "（无对客消息，符合预期）")}</pre>')
        html.append(f'<a href="../../output/new-routes-20261014/{case["result_file"]}">完整测试数据</a></details>')
    html.append('<h2>上线前保留的边界</h2><ul><li>Excel A41允许按当地情况调整顺序但不减少景点，8日第2/3天先后不再作为缺价阻塞；10日林芝酒店分店差异仍待确认。</li><li>网页标为待确认的供氧时数/浓度/移动氧舱不写入新线对客承诺；不把旧线路事实借给新线。</li><li>本次只新增两条常规线路，冬游未启用。原桃花线路的已确认活动价与配置保留，没有被标准价覆盖；不自动叠加未明确计价单位的折扣。</li><li>按本轮准备范围保留原全局沉默与静默交接要求，新线使用月份30秒；不执行网页旧版“已读当回答、半天催LINE”逻辑。</li></ul>')
    html.append(f'<h2>部署与回滚准备</h2><p>待发布包包含代码、两条线路配置、业务来源及20份原图；全包文件已逐项校验SHA-256。部署顺序、上线检查与回滚步骤已准备，实际部署及生产渠道验收尚未执行。</p><p><a href="../../output/new-routes-20261014/tibet-routes-20261014-ready.zip">下载待发布包</a> · <a href="../../output/new-routes-20261014/deployment-plan.json">部署/回滚步骤</a> · <a href="../../output/new-routes-20261014/acceptance-summary.json">验收汇总</a> · <a href="../../output/new-routes-20261014/{log_path.name}">{passed}项测试日志</a></p><p>本次模型测试 ¥{cost:.4f}；沿用之前20元账本，累计已计费/保留额 ¥{ledger["charged_or_reserved_cny"]:.4f}，未超预算。历史未知请求保留额未重置。</p>')
    html.append('<h2>原图与跟进素材</h2><details><summary>查看32张图片的对应关系</summary><div class="grid">')
    assets=json.loads((RELEASE/'asset-manifest.json').read_text(encoding='utf8'))
    for a in assets:
        path=next((OUT/'staging-v3/media').glob(a['sha256']+'.*'))
        link='../../'+path.relative_to(ROOT).as_posix()
        html.append(f'<figure><a href="{esc(link)}"><img loading="lazy" src="{esc(link)}" alt="{esc(a["display_name"])}"></a><figcaption>{esc(a["route_variant"])} · {esc(a["content_group_key"])}<br>{esc(a["source_filename"])}</figcaption></figure>')
    html.append('</div></details></html>')
    (ROOT/'docs/development/new-tibet-routes-20261014.html').write_text('\n'.join(html),encoding='utf8')
    print(json.dumps({k:v for k,v in summary.items() if k!='model_cases'}))


if __name__=='__main__':main()
