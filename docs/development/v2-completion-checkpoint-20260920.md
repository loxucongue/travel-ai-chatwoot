> 2026-09-21最终状态：已部署版本v2-trust-final-20260921T115421Z，并发布审核后的网站知识与高原PDF；真实发送关闭，出站45→45，V1保留。最终清单与证据见[v2-business-final-report-20260921.md](v2-business-final-report-20260921.md)。下文历史状态以此说明为准。

# 当前续作状态：候选27完整冻结验收运行中

- 用户追问“差不多了吧？还没优化完成吗？”：已坦诚回复主体改造完成，但完整验收发现审核误判，不可提前声称全部达标；继续收敛，不扩大重构范围。
- 当前session 11627：output/v2-completion-20260920/run_final_27.py，4 workers、10 jobs。源码冻结，禁止修改app/scripts/data/frontend src直到完成。
- release reception-v2-feedback-20260920-41e2511a6cbd53c8；source 2700a684b66cd386c0c973feaedfd8b5a729abca0a1805db6ed010008e328696。
- 全批：398 singles、108 journeys、615 verifier（123×5）、40 service、全后端、20 V1。manual-review-final-27和manual-journeys-final-27已初始化，尚未读任何27实际输出。
- 包184文件校验通过；前端49文件与已通过版本相同；未部署、真实发送0。
- 候选26完整拒绝：单轮398/398自动及人工，普通287 P50=5.562 P95=11.765；journeys105/108、r1人工23/24；verifier594/595；service38/40自动39/40人工；后端1624 passed665 skipped。所有单轮、24条r1、40service、20V1均已逐文读完并存记录。
- 候选26失败根因与修正见output/v2-completion-20260920/pending-final26-review.md。候选27已合入2app文件task_scope/reply_scope，3测试文件，export元数据；没有改数据/配置/前端。
- 修正：条件性已知供应不等于其余不供应；服务用途解释不等于新任务；客户主动追问优先于历史去重；简短线路概览不自动要求资料事件；相关性修剪不得删掉保留报价的注册事实条件。服务正则补未含等义，保留反例。
- 暂存candidate27b-source为合入来源；其中reply_fact_verification.py与实际相同，不需要重复制。注册事实条件从ROUTES按allowed事实ID取，不能从web context_fact_map取，那里没有线路条件。
- 修正探针：氧气40/40 +反向20/20 +121全审核一次；用途20/20；no_line完整5/5、global_stop完整5/5，所有实际回复已读；flight最初4/5失败保留，修正取注册条件后5/5，实际已读；新工程9/9，合入后26+27工程30/30。
- 只有full27的session 11627仍需跟踪；之前探针及full26均完成。不要结束为部分报告，继续验收有问题再修复。没有业务批准的PDF不得标已批准；该外部依赖保留。

---

## ACTIVE FULL26 — source frozen, keep working autonomously

Session46495 run_final_26.py ACTIVE,4workers10jobs:398single,108journey,595verifier(119cases),40service,fullbackend,20V1. Release reception-v2-feedback-20260920-243457da306a5d88 source6e21a2e51a264aee9ba103dd8ee9911816e2a310bec8a93da802573eaaeb6f6c. DO NOT edit app/scripts/data/frontendsrc until all10jobsdone. Export184files/archive/source hashes verified;49frontendfiles samebytes;knowledgecandidate copied. acceptance-status running_not_approved. Manual26single/journey initialized0; none read yet.

Candidate26 nowAPPLIED6appfiles (runtime,task_scope,reply_revision,claim_guards,reply_scope,model_gateway),2scripts(case synonyms+exportmetadata),21newengineeringtests,119verifiercases(exceptionartifactwriter),two22/23tests scopeargs updated. All119 fast-scope diagnostic passed;actualrelated88passed. Previous candidate26-source stage matches appliedfiles. New model_gateway connectretry <=1/node within originalrequestdeadline, not read/stream. FAST scope recheck sameprompt/parser disabledthinking1800, no6sreasoningwait. Requiredhotelshortexception protected against repeat AND unrequestedtopic only; independentfactaudit retained. Hotelperclause exclusion guard and routecaptionrepair; obligationvsunknown-task distinction; semicolon exactprune safeconditions; negative 建議不 and 不代替 synonyms.

Full25 complete rejected:393/398auto393manual,108/108journeysauto23/24manual(health65unaskedtaskpromise),574/575verifierConnectError,40serviceauto/manual,1603backend645skip. P50=5.828 P95=16.235FAIL. All398single/24r1/40service/20V1 read+recordedcomplete. MissingverifiererrorJSON reconstructed and explicitlyannotatedoriginalXML/log. Allfixes/probes below and pending-final25-review.md. Newbusinessreport/ledger still25historical/inprogress,update final26later. No realcustomer messages/deployment. PDFbusinessapprovalpending, serviceknowledgeunpublishedproduction. CONTINUE through failures, don't endpartialfinal.

## LATEST: full25 COMPLETE REJECTED; candidate26 staged, fast-scope probe active

Full25 session93842 done all10jobs, sourceunchanged980f3b3dd2b46f52c9377c5b4a92f89bc404cb048382c73cfd1fc6e1e96f1452/releasef550246d5bbe0ff4.393/398singleauto AND manual (medicine_1#4 falsepositive manuallypass; hotel_negation#1 contradictoryclause manuallyfail),108/108journeysauto23/24r1manual(health65unaskedconfirmation),574/575verifier(oneConnectError reconstructedmissingJSONwithannotation;log/XMLoriginal),40/40serviceauto/manual,1603backend645skip. P50=5.828,P95=16.235FAIL. All398replies/24r1/40service/20V1 read and recorded, acceptance-status REJECTED.

Candidate26 output/candidate26-source NOTAPPLIED: runtime negative ??? synonym; task_scope known submit obligation != unresolved task; reply_revision independent trailingsemicolon pruning(provedprior no danglingconditional) +routehotelcaptionrepair+copyguardallow hotel exception; claim_guards each explicit Bomi-only coverage quantifier cannot contradict earlier Bomi/Everestexceptions; reply_scope requiredshortexception protected from repeat/unrequestedtopic (freshfactproofremains) +FAST scope recheck sameprompt/parser THINKING DISABLED1800 instead of6sreasoningthenfallback; model_gateway oneconnectspecific retry within originalREQUEST deadline, no fastretryread/stream; scripts v2_acceptance_cases ??? synonym; exporter metadata; two22/23tests now expect fastscope args. test_candidate26_regressions21tests; test_candidate26_verifier_acceptance119cases595 and writesexceptionsartifact. run_final_26.py ready NOTSTARTED.

Probes26 age30/30passed;actualage/tips/medicine20/20allread;hotelactual15/15allread;hotelverifier first19/20 falseunrequestedexception then scopefix final20/20passed. Engineering stagedpre-final1611passed397skip (BEFOREtransport/hotel/fastscope changes, notfinalproof); related335beforefastscopepassed; latest51 (new21+updated22/23tests)passed. ONLY ACTIVE probe session61388: verifier-probe26-fast-scope ALL119 once, latest32done0fail; MUSTFINISH before applyingactual26 because per-case actualsourcefingerprint. Noother activeexec except this. Need readfailuresifany, correctstaged; afterpass apply26all .py excludingtestspropermap,copynew21tests+119verifieractual,runactualrelatedchecks,export/freeze/full26same398+108+595+40+fullbackend+20V1. Never combine bestversions.

No realcustomer sends/deployment. PDFbusinessapprovalpending, serviceknowledgeunpublishedproduction. User expects autonomouscompletion,no partialfinal. Newbusinessreport/ledger top still25needsfinalupdate. Pending25review lists allrootcauses. Priorcheckpointbelowhistorical.

## LIVE: full25 active session93842, candidate26 staged (not applied)

Actual source FROZEN980f3b3dd2b46f52c9377c5b4a92f89bc404cb048382c73cfd1fc6e1e96f1452, releasef550246d5bbe0ff4; full25 all10jobs must finish before app/scripts/data/frontendsrc changes. Export184verified,frontend49samebytes,knowledgecandidate copied,acceptance-status running. Manual singles0..253 read+recorded; medicine_1#4 corrected MANUAL pass for explicit refusal ???????; auto failure preserved. Other fails age75_1#5 unnecessaryhandoff,age76_1#3 semanticallycorrect ????? guardmisreject,tips_1#1 finalunsupported/unaskedtailcouldnotprune. Manualr1 opening9,switch_up,party_correction recorded; remaining21 needread. Verifier480/575sofarallpass,journeys33/108allpass.

Candidate26 output/candidate26-source: runtime equivalent ?????; task_scope distinguishes customer submit-certificate obligation from unresolved consultant task; reply_revision can remove independently rejected final clause after semicolon only when previous complete answer proved/no dependent conditional; scripts/v2_acceptance_cases includes ??? refusal synonym. Stagedunits8new+16old24passed; earlier failed fixture and patch errors fixed. Stagedverifier118cases590:6ageboundaries30/30passed(session4840done). Stagewrapper26script adds stagedscripts path. Activeprobe session17665:20actual singles age75_1,age76_1,tips_1,medicine_1x5, read replies when done. No26 source edits while probeactive. Staged8tests test_candidate26_regressions.py; schemafixtures test_candidate26_verifier_acceptance.py. Need fullrelatedregression afterprobe and finish25beforeapply26.

New report docs/development/v2-business-completion-report-20260921.md maps22businessfamilies+newcustomerflow, explicitly25running, mustupdate finalversion/results. Ledger top updated25. Full24 complete rejected397/398singleauto/manual,103/108journey23/24manual,556/560verifier,39/40serviceauto40manual,all20V1read. No realcustomer sends/deployment. PDFbusinessapprovalstillpending. CONTINUE autonomously, do not end with partial final.

## Latest: candidate25 applied, full acceptance pending

Full24 completed all10jobs source unchanged; rejected397/398 singles,103/108 journeys,556/560 verifier,39/40 service auto (40 manual; airfare synonym false negative). All actual398 replies,24 r1 journeys,40 service,20 V1 read and recorded. Candidate25 applied9appfiles,16tests,115verifiercases; staged1598tests passed. Probes rail5/5,appointment5/5,shape3/3,optout10/10,boundaries40/40. No customer sends or deployment. Next actual targeted checks/export/freeze/full25; business PDF approval still pending.

## ACTIVE FULL24 — continue autonomously, no partial final

Full24 session1431 RUNNING. Source FROZEN 627c4927d40348bfa20855a03abd34475b4269f1f6a9b8da7694a5e517d6b450 / release reception-v2-feedback-20260920-f1230a1dfa3a03d8. 398 singles,108 journeys,560 verifier(112 cases),40 service,fullbackend,20V1;4workers10jobs. DO NOT edit app/scripts/data/frontendsrc until all finish. Export183files/archive/hashverified,frontend49samebytes,knowledgecandidate copied. No deployment/customer send.

Full23 all10jobs complete sourceunchanged REJECTED:393/398singleauto392manual(allread),106/108journeys23/24manual(allread),546/550verifier,40/40serviceauto39manual(allread),1565backendpass1fail620skip. V1 all20read13auto; railoutstillwrong despiteauto. Both journeyfails new11 proactive repeated midclause. Full23 artifacts preserved acceptance-status rejected.

Applied24:8appfiles(runtime,budget,scope,revision,task_scope,fact_proof,route_packages,newfact_conditions),2data(serviceequipment scope restoredfromoriginalsnapshot;9dayprice declarativeamountconditions),exportmetadata.16newregressions+112verifiercases. Existing18 regression updated to assert conservative modelrepair of unsupported leading comma clause, preserving same required final task; does not require unsafe mechanicalcut. Newunitoxygen mocks task verifier too (no accidentalpaidcalls). Finalactual76 tests passed; earlier staged143/256 passed.

Probe24: brandfinal10,agefinal20,age-task20,doctor-scoped10,oxygenfinal15,doctor/group15 allpassed and actualreplymanualread. New11proactive3/3allread, exactmiddleclause removal preserves hotel exceptions; flightservice5/5allread,flightverifier20/20. Oldfailedprobespreserved. New24route answer_conditions literal whenamount→require6party/startingprice; validatepackage boundedstrings; no keywordflow.

Next: monitor full24;readALL398actualreplies customer/rendered_text/decision-action-materials (never wholesnapshots) andrecordmanual-review-final-24.json (helperrecord_read_reviews.py);24r1journeys/40service/V1;stagefuturefixes OUTPUTonly. First18rows replyread butneedcustomerandrecord; no manual24fileyet. Timingincludeallfailedrows;ordinaryP50<=8P95<=15. Full23ordinaryP95=18.156failed;24narrowedrechecks intendedimprove. PDF M07 businessapprovalstillpending; neverclaimdraftapproved. Continueuntiltechnicalcomplete insteadofpartialfinal.

## LATEST LIVE STATE 23/24 ? continue, no final yet

Full23 session57006 active,actual source FROZEN6774a306bd3c3e43fa384b5f216aba845032c83b1ce24872e88a4ec24aa56d35 / releasefcf1795bfb6d530b.4workers10jobs. Singles390of398read+recorded384manualpass(5auto failures+doctor1#3manualextra). Verifier519of550515pass; journeys45of108allpass. Manualr1recorded opening9,opening11,switch_up,switch_down,party_correction,rail,contact_time,culture,date_correction,permit_transfer. Source23mustfinishalljobsbeforeapply24.

Candidate24 staged OUTPUT only: runtime(preciseagefocus+trace),budget(contextlocalerrortrace),reply_scope(optionalrecheckonlyquestion/repeat,semanticoxygen_supply_requestmode+knownportablecoverage),reply_revision(supported-prefixrequiredtailprune+brand-onlyfocus),task_scope(mandatoryindividualconditionbeforeomissiontest),fact_proof(coordinatedsubjects?attributes). Staged service-knowledge.json restores original webpage source detail guide carries devices,notallhotel; original snapshotlines11/54 verified. Wrappers run_staged24_tests/script inject stagedSERVICE_FACTS beforeimports;actualdata untouched. 9newregressions in test_candidate24_regressions.py;112verifiercases(560) in test_candidate24_verifier_acceptance.py (notcopiedactual).

Full23failures:brand2#3,age64_2#3deadline,age75_1#5,age75_2#1,heldout_group8#5scope_timeout;doctor1#3 expandscar tohoteldevices. Verifier age76individual#3,oxygenconcise#2/#5,oxygenmissingportable#1. pending-final23-review.md records.

Probes24: brandfinal10auto/manualpass (initialmanual9/10fragmentfixed); agefinal20auto/manualpass; age-task20/20; doctor scoped10/10 after source recovery and valid positive now explicitguide/roomquery+guidecarrieddevices. Earlierfailedprobespreserved. Latest256engineeringpassed. Active5562oxygenfinal15controls,39283doctor/group15singles. Don'tmodify staged fileswhileprobesrunning. Need readprobeactualreplies,recordpasses,finishfull23manual+alljobs,reject23,apply24data/app/exportmetadata/tests,freeze/runfull24/export. Proactivepreservedexception guard23alreadyactual;full23continuoussofarallpassed. No realcustomer messages/deployment. PDFbusinessapprovalstillpending,notclaimM07complete.

## ACTIVE FULL23 ? SOURCE FROZEN

Applied7appfiles+exportmetadata,13newregressions,110verifiercases(550repeats). Related232passed. Full23 runner started,10jobs4workers,398singles108journeys550verifier40servicefullbackend20V1. release reception-v2-feedback-20260920-fcf1795bfb6d530b source 6774a306bd3c3e43fa384b5f216aba845032c83b1ce24872e88a4ec24aa56d35. Export182files/hashverified. DO NOT mutate app/scripts/data/frontendsrc until alljobsfinish. Probes healthfinal2 interrupted after3/5completed(all3passed and read); full23 covers5. Proactive corrected runtime-contract10/10passed,flight20/20+5repliespassed. Full22rejectedfully, allmanualdone. No customer sends/deploy.

## LATEST: FULL22 complete; candidate23 staged, NOT APPLIED

Full22 session68211 finished all10 jobs, regression passed; actual source still22. Singles397/398 auto393/398manual complete; verifier492/495; service39/40; journeys count pending final record (4 observed fails). All failed evidence preserved. Need finish r1 manual journeys (email/wechat unread; attachment/hotel_exception/intro11/price_scope read not recorded, intro11 sections still compare) and40service manual. Candidate23 staged7appfiles plus104verifiercases/newtests; do not claim applied. Probes new transfer/age/health/scope/opening all passed final subsets except manual health max-altitude invention fixed afterward and5negativecontrols passed. See pending-final22-review.md.

NEW diagnosis: healthr4 proactive_run repeats Rongbuk facilities; blanket repetition→full replan removed necessary Everest exception. Staged partial-span copy repair permits repair when actual nonrepeated text survives; full repetition still planner, fresh audit mandatory. Scope preserves required exceptions separately from repeated facilities. Serviceflight-2 misclassifies published capability as booking fulfillment: shared semantic task boundary strengthened, explicit fare request still handoff. Need probe both and engineeringtests, then apply23/export/runfull23. No customer sends/deploy. Keep working until technical completion, not a partial final.

## ACTIVE FULL22 — 2026-09-21

- Session68211 run_final_22.py active,4 workers10jobs,code FROZEN.
- Release reception-v2-feedback-20260920-925e8fb51c5ea5f5; source87b15167f9864699da6d0c8b15f9cd11c5fd08bd439bda722fdc548cc739e928.
- Export release-final-22,182file archive and all hashes verified. Pending business PDF only in manifest; no deploy/customer send.
- All22staged files now applied (runtime,scope,claim_guards,reply_revision,evaluation script,export metadata);17newengineeringtests and99verifiercases. Coverage recheck15/15 realmodel,related186engineering pass. Intermediate failedprobes preserved.
- Full22 expected398singles,108journeys,495verifier(two disjointshards50+49cases),40service,fullbackend20V1informational.
- Manual final22 rows0..5 read and recorded. Continue reading actual customer/rendered_text/action/assets (never entiredecision snapshots). Do not read active.progress.json in PowerShell. Record only readrows via record_read_reviews.py22startend; setcompleteTrue atlast398.
- Prior21 complete rejection and all counts below. No final completion until technical issues closed. User asked autonomous finish; no midtask final or asks whether to continue.

## Current checkpoint: 2026-09-21 candidate22 preparing (21 fully rejected)

- Full21 source 2ae735f65f8ab05ee8563e7cde4d2f19bdefad182fc4ffdd32d532cd0163b3fc preserved:398/398 auto,394/398 manual;106/108 journeys,23/24 r1 manual;462/470 verifier;39/40 service;1536 backend pass540skip. All9 jobs finished, source unchanged. No customer sends.
- Candidate22 actual: runtime,claim_guards,reply_scope,reply_revision,evaluator modified;17 regression tests staged (15 applied),99 verifier cases (495 repeats). Pending staged scope coverage-disagreement recheck and2 tests need applying after probes finish.
- Fixes: first opening compile before parser; age-anaphora certificate waiver; permission-to-inquire vs actual material request; repetition schema/reference and explicit clarification; qualification repair retains known provision; scope ordinary-arrival/oxygen concise-answer boundaries.
- Probes: opening11 3/3; age10/10; permission+actualimage15/15; new verifier20/20; hotel3/3; repetition15/15; oxygen service10/10 all replies read; staged engineering17/17. Intermediate arrival/oxygen probe rejected, preserved, see pending-final21-review.md for fixture rationale correction.
- Active probes session97108 oxygen final (actual source unchanged),session coverage created after this checkpoint. Do not mutate source during active probes.
- Prepared run_final_22.py: four workers, two verifier shards disjoint99 cases (495 repeats),398 singles,108 journeys,40 services,full backend,V1 informational. NOT STARTED. Export release22 after final source and validate hashes.
- Full21 manual all completed,service40read;V1 20read (auto13/20, obvious extra vehicle/rail errors); no final completion report yet. PDF still business approval pending; no deployment/channel sends.

# 续接重点：完整21仍在跑，隔离22已修两个新问题

请不要停止在阶段结果。用户要求全部完成后才结论；无真实客户消息、未部署。

- full21 session6502正在跑，源码仍冻结：2ae735f65f8ab05ee8563e7cde4d2f19bdefad182fc4ffdd32d532cd0163b3fc，release f8cf13616d525a58。约165条单轮自动全通过，但人工第119条age64_1#3是错误豁免，已标不通过；manual-review-final-21已经记录0–149（150条、149通过）。其余待继续阅读。连续旅程36份中opening11 r1首轮您好deepseek_reply_missing；其他无已知失败。核验131/470目前无失败。完整9jobs必须等结束再动app/scripts/data/frontendsrc。
- 隔离22在output/v2-completion-20260920/candidate22-source/app/reception_v2，只有runtime.py和claim_guards.py，基于实际21新复制。run_staged22_tests.py可测试。runtime先将无绑定、无事件、无assistant历史且delivery_intent=opening的运营已发布开场正文编译后再通用parse；reply为空而reply_body非空时同文归一，独立问题审核不变。claim_guards补证件上下文“您这个年龄不用/这个年纪不需要提交”省略宾语豁免，排除“不用担心”和否定豁免。问题及证据pending-final21-review.md。
- test_candidate22_regressions.py在output：14新测试，含真实问题伪装opening仍审核拒绝、前置路线/历史/问题边界、缺字段、年龄省略6正反例、模型误通过时确定性拦截。加已有21/release相关共127通过（session99011结束）。opening11真实连续旅程3/3通过，output/journeys-probe22-opening（session34924已结束）。这个probe在新claim_guards文件建前已启动，不当年龄验证。尚未把22写入实际app。
- full21的单轮从150继续读；第一轮旅程尚未开始人工记录。full20各项已完整拒绝与归档，下文。

---

# 当前最新：候选21已合入并冻结完整验收

完整运行session6502，runner output/v2-completion-20260920/run_final_21.py。release reception-v2-feedback-20260920-f8cf13616d525a58；source 2ae735f65f8ab05ee8563e7cde4d2f19bdefad182fc4ffdd32d532cd0163b3fc。398单轮+108连续旅程+470核验正反对照+40服务+全后端回归+20条V1比较（非门禁）。四个app文件已合入，medicine“不便”断言已修正。合入后137工程测试通过。导出182文件，archive/hash逐个验证；release-final-21/acceptance-status.json是running_not_approved。严禁现在改app/scripts/data/frontendsrc；可以写docs/tests/output。当前没有其他实验在运行。

候选21最新素材修复：caption内容改为actual_answer_segments编号引用；重复必须引用另一条真实current/history片段；同时长段高相似的正文/配图重复保留数字和否定条件一致校验。新增失败探针都保存了，最终10/10正反例通过才合入。人工21尚未开始；manual-journeys-final-21初始化。完整20已拒绝且所有记录完整，详情在下面和pending-final20-review.md。

继续等待并逐条阅读21实际回复，发现失败记录且隔离修复，完整冻结验收结束才能再次改源码。不能部分失败就结束让用户继续。用户禁止真实消息，不部署。PDF业务审批和真实渠道收据仍是外部边界，不得伪造。

---

# 最新检查点：候选20完整拒绝，候选21隔离修复中

候选20源码全程冻结 `576ea0c96a79154079be3ecd207bb39b9ae8437e4f3bdb4791f9553a02892185`，完整9个任务结束。自动单轮395/398、人工392/398；连续旅程105/108、人工21/24；对照核验436/440；服务39/40；后端1514通过510跳过；前端构建及1测试通过。候选20已在release-final-20/acceptance-status.json标记rejected。全部原始失败保存，详情pending-final20-review.md。

实际app源码尚未合入21（等隔离探针结束，避免源指纹断言误失败）。21副本在output/v2-completion-20260920/candidate21-source/app：最终只删无依据尾句的非生成收尾；主动事实证据补全但候选新价值不扩大；附件元数据不当重复；事实推理复核6秒有界快速回退；64/75岁条件适用；路线+资料更新回执语义统一；省略主语帮您/替您的动作候选仍交独立需求证明；随身氧气已知配置和5000米条件都保留。工程测试复制到backend/tests/test_v2_completion_final21_regressions.py，新增6个完整验收对照（总94×5=470）。

21实验通过：opening9旅程3、date_correction旅程5；64岁对照15；75岁正反对照15；真实重复/元数据对照10；优惠主动承诺与必要核对对照15；氧气服务5。工程121通过（新加6条动作候选测试尚需再跑）。当前活跃：profile旅程session3922（age_boundary5+price_scope3+email_capture5），新正式fixture对照session65641（9条）。runner21已准备，不要在这些测试结束前修改app源码。下一步读完实验实际答复、合入21四个app文件，修正medicine不便断言，跑必要工程检查，再冻结完整21，导出同源release并全量人工审阅。

用户禁止真实客户发消息，尚未部署。PDF仍待业务审核，不能假装获批或把缺文件降级当M07完成。

---

# 最新冻结执行状态（候选20）

用户要求持续开发、测试、修复直到达标。禁止真实客户消息，未部署；工作区不是Git。不要结束在中间失败报告。

**正在完整冻结验收**：run_final_20.py，会话66581。release reception-v2-feedback-20260920-f2b5e60a0b611f84，source 576ea0c96a79154079be3ecd207bb39b9ae8437e4f3bdb4791f9553a02892185。104种398次单轮、24种108次旅程、88种440次独立审核、40服务、全后端、20同题V1对照。9个job全部结束前不改源码。前端build与1项测试通过；release-final-20已导出182文件，状态running_not_approved。

逐文复核尚未开始，manual-review-final-20.json、manual-journeys-final-20.json已初始化0。record_read_reviews.py只记录真正已阅读的单轮范围，不能批量伪审。最终全量源哈希必须一致，不拼接历史最好成绩。候选副本candidate20-source已经落后于最后的scope措辞，不可再次覆盖源码。

候选19完整结果已拒绝：单轮386/388自动、379/388逐文；旅程104/108自动、23/24逐文；核验354/355；服务39/40；后端1486通过425跳过。原始失败与14项根因在pending-final19-review.md。

候选20实际改动：原子属性且保留完整证据条件；品牌/等级、药品处方/供应、优惠存在/资格、起价与固定价各自证明。第一人称核对与条件式查询也需独立需求证明；核对内容取自实际承诺原句，不允许审核器虚构额外工作。通用咨询与具体履约分开，客制规划是服务请求而非索取现成附件。资料图文去重复。事实修复显式空任务许可。人数+线路回执保留route事件。原文证据验证移入既有JSON修复。一次最多一个信息项，允许真实客制需求只问一个人数。Windows进度原子替换共享冲突有限重试，不吞永久错误。

定向证据：191项后增至199项工程通过；24重点单轮逐文通过；18连续自动通过但手工发现条件式查询扩展后继续修复；最终probe20b六单轮/15审核/6服务/5旅程自动通过，手工发现客制问人数+天数后补一项信息合同；probe20e六实际单轮均逐文通过，核验单问人数误拒已修复，probe20f6/6正反例通过。隔离probe20b-verifier-staged60/60、probe20c30/30、probe20d有正例误分类已修。均非完整发布验收。

高原PDF仍是output/pdf/china2go-altitude-guide-review.pdf待审草案；此前已问批准但没有回复。禁止伪造批准。知识本地pending_review revision5（31事实10模块），已发布rev3仅演练。最终要完成技术闭环再列出具体业务批准/真实渠道收件的剩余边界；不能拿模拟成功冒充M07已批准。

# V2 当前整改状态（持续更新）

本页替代最初阶段报告；开发明细和失败历史保留于同目录工作日志。当前正在完成统一验收，尚未给出最终发布结论。没有生产部署，也没有向真实客户发送测试消息。

## 已实现的原方案工作包

- A：本轮语义事件、原文证据、资料纠正、联系偏好、预约与考虑等待、双向 V1/V2 可信投影。
- B：按注册线路加载 Skill、事实与素材；关键词仅辅助检索，第三线路已加入工程回归。
- C：V2 默认 1/120 分钟，从实际交付起算；统一延期、不消耗下一节点、客户新消息取消旧任务、越窗处理。
- D：完整介绍的有序分段交付、独立回执、客户打断、重启和未知提交保护；缺资料进入真实人工待办；PDF 上传、预览、按哈希批准和替换撤销批准。
- E：运营配置进入实际执行，发布摘要和素材哈希检查；旧任务版本保护。
- F：三份反馈文档逐项映射，100 个单轮场景、24 条连续旅程、业务边界与表达规则。
- G：11 类执行故障、隔离数据库和实际 worker 验证；消息来源编号和逐轮资料持久化检查。
- H：30 秒总预算；100 个单轮按关键 5 次/其他 3 次共 378 次，连续旅程共 108 次；独立核验 67 种正反例、最终各 5 次共 335 次、通用服务扩展 40 次；最终同版完整回归进行中。
- I：已发布通用知识接入、事实引用和供氧条件保护；此次测试的 10 个模块/31 条事实已生成本地待审修订 5，原已发布修订 3 和演练范围不变。

## 本轮继续修复的验收失败

1. 历史人数、日期与已完成资料请求被误当本轮事件：增加当前证据过滤、独立资料保存检查、真实交付上下文。
2. 日期字段误用、资料口头确认却未保存：明确字段契约，未知字段进入修复，并验证实际持久化。
3. 审核补充的事实引用在文案修复时被拒绝：统一修复与审核的批准事实范围。
4. 文案删掉某主题却遗留对应照片：主动跟进修复同步删除失去事实关联的图片。
5. 9/11 日住宿说明遗漏珠峰例外：增加确定性业务条件保护，不能只信模型审核。
6. 主动跟进只问行程图收到了吗：缺少候选新价值时要求重新规划，不能只改写催读句。
7. 真实文案提及顾问核对却遗漏任务申报：从实际核对语句进入独立需求审核，无关承诺删除；必要核对保持人工闭环。
8. 业务确认价等内部用语泄露：加入明确表达校验。

## 最新验收状态（2026-09-21，候选18开发中）

候选17已完成并拒绝发布：单轮自动369/378、逐条Codex审阅362/378；连续旅程104/108、24种首轮人工模型审阅22/24；审核304/305；服务38/40。后端1422通过、375跳过，前端构建与现有1项测试通过。完整证据在 `output/v2-completion-20260920/*final-17*`，不能把这些成绩合并为候选18通过。

候选18正在集中修复候选17的16项发现，清单见 `output/v2-completion-20260920/pending-final17-review.md`。已实现请求种类与媒介分离、缺附件待办状态、可信绑定保留、调度事件清理、最小人工任务、文案与状态分别修复，以及同轮相同证据的任务证明复用。输出额度耗尽的审核仅在原30秒预算内按相同严格协议重试。

候选18已冻结：`reception-v2-feedback-20260920-257e8d12d023a9f9`，源码指纹`c44ef4eb2dee895597a6bd87a439069e30964273656d52b2c4bb776ef6f7cf5c`。定向代码159项通过；机票/素食/日期连续流程7/7、任务证明42/42、最后缺PDF并问小费/留下微信并问小费组合6/6通过，均保留各探针证据与历史失败。

完整验收现在进行中：102种共388次单轮、108旅程、335审核、40服务及全后端回归；前端构建和现有1项测试已通过。`final-18-status.json`与`release-final-18/acceptance-status.json`为状态入口。开始逐条阅读实际回复，不能将中途全绿视为最终通过。

生产未部署，真实客户消息发送数为0。高原说明PDF仍为待审草案，不能当已批准附件；通用知识本地待审修订5尚未发布。
