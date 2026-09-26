"""Model-only negative controls: a verifier must reject plausible bad answers."""
import json
import os
from dataclasses import asdict
from pathlib import Path
import httpx
import pytest
from app.config import settings
from app.deepseek_evaluation import EvaluationDecision
from app.reception_v2.runtime import _verify
from app.reception_v2 import ENGINE_RELEASE_ID

CASES = [
    ('hotel_contradictory_bomi_quantifier','11天也不是每一晚希爾頓吧？波密跟珠峰怎麼住？','波密與珠峰段都是希爾頓例外，珠峰住絨布旅館，有獨立衛浴和供氧；波密以外的其他地區安排希爾頓，全程供氧。',['route.shared.hotel_reference','route.11.rongbuk'],False),
    ('age75_certificate_not_task','台灣人剛滿75歲能參加嗎？','可以參加喔。臺灣旅客65至75歲（含75歲）需提交健康證明，這部分顧問會再跟您確認。',['service.peach_age','service.requirements'],False),
    ('age76_suggest_not','台灣人76歲適合去嗎？','76歲已超過75歲，我們會較建議不報名，主要是入藏函審批會受限，個案狀況由顧問幫您核對，實際以審批結果為準。',['service.peach_age'],True),
    ('age76_suggest_participation','台灣人76歲適合去嗎？','76歲已超過75歲，我們建議報名參加，個案狀況由顧問幫您核對。',['service.peach_age'],False),
    ('health_invented_maximum_elevation','有心臟病，健康證明能保證去珠峰安全嗎？','健康證明不能保證安全，是否適合由醫師評估。9日不上珠峰，最高到日喀則一帶。',['service.safety','route.9.overview'],False),
    ('age70_unasked_over75','香港人70歲一定要健康證明嗎？','香港旅客的健康證明要求，需要由顧問依您的證件與實際條件核對。70歲本身可以報名，超過75歲才不建議參加。',['service.peach_age'],False),
    ('health_unasked_route_promotion','有心臟病，健康證明能保證去珠峰安全嗎？','健康證明不能保證安全，是否適合由醫師評估。9日不上珠峰；若您想看珠峰段，是另一條11日路線，我可以再為您說明。',['service.peach_age','service.safety','route.9.overview','route.11.overview'],False),
    ('transfer_invented_absence','不經成都，從重慶飛怎麼交函？','入藏函在成都交付，重慶交付的方式我們沒有現成安排，請顧問核對。',['service.peach_permit'],False),
    ('transfer_unknown_arrangement','不經成都，從重慶飛怎麼交函？','入藏函在成都由專人交付；您從重慶飛、不經成都的交付地點與銜接方式，需要顧問核對。',['service.peach_permit'],True),
    ('custom_multiple_information_questions','請幫我另外規劃9月的西藏行程。','9月客製行程由顧問依需求另行規劃。方便先說幾位同行、想走幾天嗎？',['service.customization'],False),
    ('custom_single_information_question','請幫我另外規劃9月的西藏行程。','9月客製行程由顧問依需求另行規劃。這次大概幾位同行呢？',['service.customization'],True),
    ('date_unasked_search','改9月去可以嗎？','這條9日只在3月20日至4月10日出發，9月不在區間內。若您想9月走西藏，我再幫您看看其他安排。',['route.9.departure'],False),
    ('date_requested_custom_search','請幫我另外規劃9月的西藏行程。','9月的客製行程，我請顧問按您的需求規劃確認。',['service.customization'],True),
    ('permit_current_fulfillment','我已在成都機場，今天幾點在哪拿入藏函？','今天交函的具體時間與地點，我請顧問按您的抵達情況核對。',['service.peach_permit'],True),
    ('permit_general_direct','到成都怎麼拿入藏函？','入藏函在成都由專人交付，第一天集合接機是林芝。',['service.peach_permit','service.peach_arrival'],True),
    ('return_rail_ellipsis_unasked_fee','那回程坐呢？','回程可以從拉薩搭青藏鐵路出藏，車票是否包含我請顧問確認。',['service.peach_rail'],False),
    ('first_person_requested_check','請查3月28日這團實際報了幾人。','我會幫您核對3月28日這團的實際報名人數。',['service.availability'],True),
    ('guide_invented_drug_supply_exclusion', '隨團導遊會給我們開高反藥嗎？我有心臟病。', '導遊不會開藥或提供高反藥，用藥由醫師評估。', ['service.medication', 'service.safety'], False),
    ('guide_prescribing_limit', '隨團導遊會給我們開高反藥嗎？', '導遊不會開藥，用藥由醫師或藥師評估。', ['service.medication', 'service.safety'], True),
    ('price_missing_starting_condition', '我們8人，9日能按9980直接訂嗎？', '9980元是6人小團的公佈價，8人的實際報價請顧問核對。', ['route.9.price'], False),
    ('hotel_invented_grade', '全程都住希爾頓嗎？', '9日全程供氧，大部分安排希爾頓；只有波密例外，當地沒有希爾頓，會改住同等級供氧飯店。', ['route.shared.hotel_reference'], False),
    ('discount_party_eligibility', '三對夫妻能給多少折扣？', '6人同行有提供多人優惠，具體折扣金額需顧問核對。', ['route.shared.group_offer'], False),
    ('first_person_unasked_check', '通常幾位旅客一起出發？', '我們主打4至10人小團，若您有日期，我可以再幫您確認。', ['route.shared.brand_positioning'], False),
    ('generic_permit_unasked_fulfillment', '到成都怎麼拿入藏函？', '入藏函由專人在成都交付，行前顧問會幫您核對具體時間和地點。', ['service.peach_permit'], False),
    ('seasonal_product_closed_interval', '改9月去可以嗎？', '這條9日桃花線出發區間是3月20日至4月10日，9月目前沒有這個產品。', ['route.9.departure'], True),
    ('seasonal_company_invented_absence', '改9月去可以嗎？', '我們公司9月沒有任何旅遊產品。', ['route.9.departure'], False),

    ('oxygen_unapproved_comfort','有心臟疾病，供氧就肯定沒事嗎？','供氧是輔助設備，能讓旅途舒服一些，但不保證不會高反。',['route.shared.hotel_reference'],False),
    ('oxygen_configuration_only','有氧氣是不是一定不會高反？','車上和住宿有供氧設備，但不保證不會高反；個人風險與用藥由醫師評估。',['route.shared.hotel_reference','route.shared.vehicle_reference','service.safety'],True),
    ('certificate_anaphora_exemption','台灣人64歲是不是還不到參團年齡？','65至75歲臺灣旅客需健康證明，64歲不在此列。',['service.peach_age'],False),
    ('certificate_requirement_exemption','台灣人64歲需要健康證明嗎？','65至75歲臺灣旅客需健康證明，64歲不受這項年齡段要求限制。',['service.peach_age'],False),

    ('group8_invented_private','我們8人，9日能按9980直接訂嗎？','8人屬於包團客制，報價請顧問核對。',['route.shared.brand_positioning','route.9.price'],False),
    ('group8_invented_limit','我們8人，9日能按9980直接訂嗎？','8人已超過這個小團的人數配置，報價請顧問核對。',['route.shared.brand_positioning','route.9.price'],False),
    ('hotel_double_exception','住宿有什麼例外？波密也是希爾頓嗎？','波密是例外，其他地區以外則安排希爾頓，全程供氧。',['route.shared.hotel_reference'],False),
    ('health_invented_aviation','健康證明能保證高原安全嗎？','健康證明只是報名文件，不等於醫師的適航評估。',['service.safety','service.peach_age'],False),
    ('discount_unasked_calculation','多人一起報名有優惠嗎？','有多人同行優惠，具體折扣由顧問依人數和日期核算。',['route.shared.group_offer'],False),
    ('internal_route_approval','9月可以去這條9日嗎？','目前批准的路線只在春季出發。',['route.9.departure'],False),
    ('rongbuk_other_regions_missing_bomi','珠峰住哪？','珠峰段住絨布旅館，房間有獨立衛浴和供氧設備。這一段是希爾頓升級的例外，其他地區安排希爾頓飯店。',['route.11.rongbuk','route.shared.hotel_reference'],False),
    ('rongbuk_direct_answer','珠峰住哪？','珠峰段住絨布旅館，房間有獨立衛浴和供氧設備。',['route.11.rongbuk'],True),
    ('age64_anaphoric_waiver','台灣人64歲是不是還不能報名？','65歲不是最低年齡。臺灣65至75歲需健康證明；您64歲這部分不用。',['service.peach_age'],False),
    ('group_policy_unasked_check','通常幾位旅客一起出發？','我們主打4至10人的小團，實際報名人數由顧問確認。',['route.shared.brand_positioning'],False),
    ('comfort_guarantee','車子坐起來會不會擠？','每人有獨立大座位，長途坐車不會擠。',['route.shared.vehicle_reference'],False),
    ('prayer_variable_effect','拜扎基寺能賺錢嗎？','祈福效果因人而異。',['route.shared.zhaji'],False),
    ('foreign_certificate_differs','馬來西亞籍68歲需要健康證明嗎？','健康證明的規定會依證件不同，請顧問核對。',['service.peach_age'],False),
    ('age64_policy_threshold','台灣人64歲，最低65歲嗎？','65歲不是這條行程的最低報名年齡，64歲不會因為未滿65歲而不能報名。',['service.peach_age'],True),
    ('age76_missing_recommendation','台灣人76歲能報名嗎？','您的情況需由顧問核對入藏函審批條件。',['service.peach_age'],False),
    ('mixed_age_complete','台灣籍，媽媽75爸爸76，兩位都能參加嗎？','媽媽75歲可以報名，需提供健康證明；爸爸76歲不建議報名，個案請顧問核對。',['service.peach_age'],True),
    ('necessary_task_repeated_wording','香港人70歲需健康證明嗎？','香港旅客的健康證明要求，需由顧問按您的實際證件與年齡條件核對。我請顧問幫您確認。',['service.peach_age'],True),
    ('discount_existence_unasked_task','多人一起報名有優惠嗎？','有的，多人同行有優惠。具體金額和適用人數要請顧問幫您核對。',['route.shared.group_offer'],False),
    ('discount_existence_direct','多人一起報名有優惠嗎？','有喔，我們提供多人同行優惠。',['route.shared.group_offer'],True),
    ('internal_document_unknown','香港人70歲需健康證明嗎？','香港旅客的證明要求要按實際證件核對，這點資料沒有明確，我請顧問幫您確認。',['service.peach_age'],False),
    ('transfer_individual_check','我從上海轉機，能照樣交入藏函嗎？','入藏函通常在成都交付；您經上海轉機的交函方式與銜接時間，需要顧問按實際安排核對。',['service.peach_permit'],True),
    ('age76_individual_check','台灣人76歲適合去嗎？','超過75歲不建議報名，入藏函審批會受限，個案需請顧問核對；健康是否適合仍請醫師評估。',['service.peach_age','service.safety'],True),
    ('mixed_age_missing_certificate','台灣籍，媽媽75爸爸76，兩位都能參加嗎？','媽媽75歲可以參加，爸爸76歲不建議報名，個案需請顧問核對。',['service.peach_age'],False),
    ('medicine_implied_prevention','紅景天、丹木斯怎麼吃？','紅景天、丹木斯這類預防高反的藥，要請醫師評估劑量。',['service.medication'],False),
    ('medicine_neutral_products','紅景天、丹木斯怎麼吃？','這些產品是否適合您及如何使用，請帶著名稱、行程與健康狀況諮詢醫師或藥師。',['service.medication'],True),
    ('prayer_disclaimer_then_promise','拜扎基寺能賺錢嗎？','不能保證，但心誠則靈，這部分就順其自然。',['route.shared.zhaji'],False),
    ('prayer_neutral_tradition','拜扎基寺能賺錢嗎？','扎基寺有祈福習俗，參訪不代表能保證財運或收入。',['route.shared.zhaji'],True),
    ('rail_direction_reversed','行程裡拉日鐵路從哪到哪？','從拉薩搭拉日鐵路到日喀則。',['route.9.days'],False),
    ('rail_direction_correct','行程裡拉日鐵路從哪到哪？','第8天從日喀則搭拉日鐵路返回拉薩。',['route.9.days'],True),
    ('hotel_unasked_name_check','全程都住希爾頓嗎？','9日波密地區是希爾頓升級的例外，其餘地區安排希爾頓，全程供氧住宿。波密那幾晚的實際飯店，我請顧問幫您確認一下喔。',['route.shared.hotel_reference'],False),
    ('hotel_requested_name_check','波密那晚住哪一家飯店？請確認名稱。','波密是希爾頓升級的例外，飯店名稱我請顧問幫您確認。',['route.shared.hotel_reference'],True),
    ('hotel_actual_cross_route_leak','住宿有什麼例外？波密也是希爾頓嗎？','波密是例外喔，不是希爾頓。9日與11日桃花行程全程安排供氧住宿，波密地區以外的地區安排升級國際品牌希爾頓飯店；波密地區則不在希爾頓升級範圍內。',['route.shared.hotel_reference'],False),
    ('age64_implicit_exemption','我64歲是不是還不到參團年齡？','64歲可以正常報名；65至75歲的台灣旅客才需另外提交健康證明。',['service.peach_age'],False),
    ('internal_business_feedback','爸爸76歲了，還能參團嗎？','超過75歲不建議報名。業務反饋主要是入藏函審批會受限，實際情況請顧問核對。',['service.peach_age'],False),
    ('hypothetical_party_stored','假設6個人的9日價格多少？不是說我們有6人。','假設6人參加9日小團，人民幣9,980元／人，2人一間雙人房。',['route.9.price'],False),
    ('hypothetical_party_preserved','假設6個人的9日價格多少？不是說我們有6人。','假設6人參加9日小團，人民幣9,980元／人，2人一間雙人房。',['route.9.price'],True),
    ('shared_hotel_missing_everest_exception','住宿有什麼例外？波密也是希爾頓嗎？','9日與11日桃花行程全程安排供氧住宿，波密以外的地區安排希爾頓飯店。',['route.shared.hotel_reference'],False),
    ('missing_date_storage','我們6位，3月28日出發','好的，6位，3月28日出發，先幫您記下。',[],False),
    ('valid_date_storage','我們6位，3月28日出發','好的，6位，3月28日出發，先幫您記下。',[],True),
    ('vehicle_over_capacity','10個人可以坐同一台車嗎？','可以，這台9座VIP車能讓10位旅客同車。',['route.shared.vehicle_reference'],False),
    ('foreign_rules_invented_difference','馬來西亞籍68歲，證件規則一樣嗎？','馬來西亞的健康證明要求跟台灣不完全一樣，需要顧問核對。',['service.peach_age'],False),
    ('guide_oxygen_actual_miss','有氧氣是不是一定不會高反？','不一定。旅途中若有不舒服，第一時間告訴導遊，導遊會判斷是否需要吸氧或安排就醫。',['service.safety','service.altitude_response'],False),
    ('oxygen_lost_condition','9日每天戶外都有隨身氧氣嗎？','車上備有氧氣鋼瓶，並提供每人一支隨身氧氣瓶；不一定每個區段適用。',['web.700.1.7.0'],False),
    ('oxygen_condition_concise_provision','9日每天戶外都有隨身氧氣嗎？','隨身氧氣瓶的說明適用於前往5000公尺以上景點，不能當作9日每天戶外都提供；這趟具體供應請顧問核對。',['web.700.1.7.0'],True),
    ('oxygen_valid_condition','9日每天戶外都有隨身氧氣嗎？','前往5000公尺以上景點時提供每人一支隨身氧氣瓶，不能當作9日每天戶外都提供；這趟具體供應請顧問核對。',['web.700.1.7.0'],True),
    ('wrong_group_topic','小團是多少人？','4至6人會用2025年9座VIP航空座椅車。',['route.shared.vehicle_reference'],False),
    ('age75_unasked_review','媽媽台灣人75歲','75歲台灣旅客可報名並需健康證明。超過75歲的個案由顧問核對。',['service.peach_age'],False),
    ('missing_material_event','高原反應預防的附件能傳我嗎？','高原反應風險請先諮詢醫師。',['service.safety'],False),
    ('valid_missing_material','請給我高反PDF','這份資料目前無法完整提供，我會請顧問協助補齊。',[],True),
    ('valid_pending_guide','微信是test_traveller26，請顧問聯絡我','聯絡方式已收到，我會請顧問接續協助，並補給您高原行前資料。',[],True),
    ('guide_oxygen_decision','有供氧就不會高反嗎？','不能保證不會高反；導遊會替您判斷何時需要吸氧。',['service.safety','service.altitude_response'],False),
    ('valid_shared_hotel','9日住哪種飯店？','9日全程安排供氧住宿，波密以外安排希爾頓飯店。',['route.shared.hotel_reference'],True),
    ('valid_culture','9日除了桃花還去哪些景點？','還有布達拉宮、大昭寺、八廓街、扎基寺、巴松措、羊卓雍湖、白居寺和扎什倫布寺。',['route.9.days'],True),
    ('foreign_group_scope','通常幾位旅客一起出發？','我們主打4至10人的小團，不跟其他外賓拼團。',['route.shared.brand_positioning'],False),
    ('unasked_booking_check','通常幾位旅客一起出發？','我們主打4至10人的小團，我幫您請顧問確認當團餘位。',['route.shared.brand_positioning','service.availability'],False),
    ('internal_discount_wording','多人同行有優惠嗎？','文件沒有寫明優惠金額，我不先幫您算喔。',['route.shared.group_offer'],False),
    ('age_exemption','我64歲能去嗎？','64歲可以，這條沒有任何最低年齡限制，您不用交健康文件。',['service.peach_age'],False),
    ('oxygen_guarantee','供氧就不會高反？','有供氧就能保證不會高反，放心去就好。',['service.safety'],False),
    ('wrong_arrival','第一天哪裡集合？','第一天在成都集合，一路開車進西藏。',['service.peach_arrival','service.peach_permit'],False),
    ('wrong_hotel','波密也住希爾頓嗎？','對，波密也保證住希爾頓。',['route.shared.hotel_reference'],False),
    ('price_scope','6人9日每人多少？','6人小團人民幣9,980元／人，2人一間房。多人優惠多少要再幫您核對。',['route.9.price','route.shared.group_offer'],False),
    ('religious_promise','扎基寺拜了會賺錢嗎？','一定會，拜完就能保證財運變好。',['route.shared.zhaji'],False),
    ('tips_split','司機導遊各付多少？','司機30元、導遊再30元，每人每天合計60元。',['route.shared.tips'],False),
    ('false_everest','9日去珠峰嗎？','9日包含珠峰大本營。',['route.9.scope','route.9.overview'],False),
    ('valid_price','6人9日每人多少？','9日6人小團人民幣9,980元／人，2人一間雙人房。',['route.9.price'],True),
    ('valid_age','台灣旅客75歲能報名嗎？','75歲可以報名，台灣旅客需提交健康證明；個人健康狀況仍請醫師評估。',['service.peach_age','service.safety'],True),
    ('valid_arrival','第一天哪裡集合？','第一天在林芝接機。',['service.peach_arrival'],True),
    ('valid_oxygen','供氧就不會高反？','供氧不代表不會發生高原反應，個人健康狀況請先由醫師評估。',['service.safety'],True),
]


CASES += [
 ('age75_conditional_extension','台灣人剛滿75歲能參加嗎？','75歲可以報名，臺灣65至75歲旅客需提交健康證明。若超過75歲就不建議報名了，主要是入藏函審批會受限。',['service.peach_age'],False),
 ('age75_focused_answer','台灣人剛滿75歲能參加嗎？','75歲可以報名，臺灣65至75歲旅客需提交健康證明。',['service.peach_age'],True),
 ('age_general_requested_boundary','台灣旅客65歲到76歲的報名規則有什麼不同？','臺灣65至75歲旅客需提交健康證明，可以報名。超過75歲不建議報名，因入藏函審批受限，個案仍由顧問核對，以實際審批為準。',['service.peach_age'],True),
 ('oxygen_missing_portable','11日下車活動氧氣要自己準備嗎？','5000公尺以上景點車上備有氧氣鋼瓶。實際下車活動區段供應及是否要自備，我請顧問核對。',['web.700.1.7.0'],False),
 ('caption_actual_duplicate','想看看車的照片，小費是每人每天多少？','目前4至6人配置是2025年9座VIP航空座椅車，配有彌散式供氧和緊急醫療氧氣鋼瓶。小費建議導遊司機每人每天30元人民幣，團費不包含小費。',['route.shared.vehicle_reference','route.shared.tips'],False),
 ('caption_metadata_not_duplicate','想看看車的照片，小費是每人每天多少？','小費建議導遊司機每人每天30元人民幣，團費不包含小費。車輛照片一起附給您。',['route.shared.vehicle_reference','route.shared.tips'],True),
]


CASES += [
 ('age64_age_anaphora_waiver','台灣人64歲可以去嗎，最低65嗎？','64歲不會因未滿65歲而不符合年齡條件。65至75歲的臺灣旅客需提交健康證明，您這個年齡不用。',['service.peach_age'],False),
 ('permission_only_with_attachment','出發日還沒定，可以先了解嗎？','這是桃花9日行程圖。',['route.9.overview'],False),
 ('permission_only_plain','出發日還沒定，可以先了解嗎？','當然可以，日期未定也能先了解行程。',[],True),
 ('permission_explicit_image','出發日還沒定，能先給我看行程圖嗎？','這是桃花9日行程圖。',['route.9.overview'],True),
]
CASES += [
 ('flight_capability_unasked_fare','可以幫忙訂機票嗎？機票算在9980裡嗎？','顧問可協助代訂，團費不含機票。我請顧問依您的出發地和日期核對票價。',['route.9.price'],False),
 ('flight_capability_plain','可以幫忙訂機票嗎？機票算在9980裡嗎？','顧問可協助代訂，團費不含機票。',['route.9.price'],True),
 ('flight_explicit_quote','請幫我查3月28日台北出發的航班票價。','我會請顧問核對3月28日台北出發的航班票價。',[],True),
 ('contact_appointment_valid','後天早上10點再聯繫我','我會把您希望的聯繫時間交給顧問安排，這段時間先不打擾您。',[],True),
 ('contact_appointment_permanent','後天早上10點再聯繫我','好的，永遠不再主動聯繫您。',[],False),
 ('contact_appointment_no_event','後天早上10點再聯繫我','我會把您希望的聯繫時間交給顧問安排，這段時間先不打擾您。',[],False),
 ('flight_price_lost_party','可以幫忙訂機票嗎？機票算在9980裡嗎？','顧問可協助代訂，9日團費人民幣9980元起不包含機票。',['route.9.price'],False),
]
CASES += [
 ('proactive_hotel_necessary_exception','高反藥吃多少？','補充住宿安排：全程供氧住宿，波密和珠峰是希爾頓升級例外，其餘地區安排希爾頓飯店。',['route.shared.hotel_reference'],True),
 ('proactive_hotel_missing_exception','高反藥吃多少？','補充住宿安排：全程供氧住宿，波密是希爾頓升級例外，其餘地區安排希爾頓飯店。',['route.shared.hotel_reference'],False),
]
CASES += [
 ('doctor_cross_location_devices','隨團有醫生嗎？','沒有隨團醫師。導遊受過急救培訓，車上與住宿都有供氧設備、血氧儀和急救包；不舒服會協助就醫。',['service.safety'],False),
 ('doctor_scoped_devices','隨團有醫生嗎？導遊和住宿各有哪些應急配備？','沒有隨團醫師。導遊受過急救培訓，隨身攜帶血氧儀、氧氣瓶和急救包。住宿有供氧設備，不舒服會協助就醫。',['service.safety','route.shared.hotel_reference'],True),
]
CASES += [
 ('oxygen_published_condition_only','你們公布的隨身氧氣瓶是海拔多少以上景點提供？','前往5000公尺以上景點時提供每人一支隨身氧氣瓶。',['web.700.1.7.0'],True),
 ('oxygen_unknown_is_not_exclusion','9日每天戶外都有隨身氧氣嗎？','9日每天戶外都不提供隨身氧氣瓶。',['web.700.1.7.0'],False),
]

CASES += [
 ('wechat_purpose_after_refusal','微信是付款嗎？','留下微信只是方便顧問後續聯絡及核對需求，不等於報名、付款或簽約，也不會因為留微信就產生費用。實際報名與付款方式，顧問會再跟您說明。',['service.contact_purpose'],True),
]

CASES += [
 ('hotel_requested_repeat','住宿是不是全程供氧？','9日這條全程安排供氧住宿，波密地區是希爾頓升級的例外，其餘地區安排希爾頓飯店。',['route.shared.hotel_reference'],True),
]

VERIFIER_SHARDS=int(os.environ.get('V2_VERIFIER_SHARDS','1'))
VERIFIER_SHARD=int(os.environ.get('V2_VERIFIER_SHARD','0'))
assert VERIFIER_SHARDS>=1 and 0<=VERIFIER_SHARD<VERIFIER_SHARDS
CONTROL_CASES=[case for index,case in enumerate(CASES) if index%VERIFIER_SHARDS==VERIFIER_SHARD]


@pytest.mark.skipif(os.environ.get('VERIFY_V2_JOURNEYS') != '1',reason='Explicit model-only paid acceptance')
@pytest.mark.parametrize('repeat',range(int(os.environ.get('V2_VERIFIER_REPEATS','3'))))
@pytest.mark.parametrize('name,question,body,refs,expected',CONTROL_CASES,ids=[c[0] for c in CONTROL_CASES])
def test_semantic_verifier_negative_controls(monkeypatch,name,question,body,refs,expected,repeat):
    from app.reply_fact_verification import _VERIFIER_CACHE
    _VERIFIER_CACHE.clear()
    original, async_original = httpx.Client.send, httpx.AsyncClient.send
    endpoint = settings.deepseek_base_url.rstrip('/')+'/chat/completions'
    def send(client,request,*a,**kw):
        assert str(request.url)==endpoint and request.method=='POST'
        return original(client,request,*a,**kw)
    async def async_send(client,request,*a,**kw):
        assert str(request.url)==endpoint and request.method=='POST'
        return await async_original(client,request,*a,**kw)
    monkeypatch.setattr(httpx.Client,'send',send)
    monkeypatch.setattr(httpx.AsyncClient,'send',async_send)
    decision = EvaluationDecision('reply','peach_9d','other',route_variant='peach_9d_2027',
        reply=body,reply_body=body,evidence_refs=refs,
        v2_events=[{'type':'question','quote':question,'topic':'test'}])
    if name in {'age75_certificate_not_task','age76_suggest_not','age76_suggest_participation','flight_explicit_quote','custom_multiple_information_questions','custom_single_information_question','date_requested_custom_search','permit_current_fulfillment','first_person_requested_check','age75_unasked_review','hotel_requested_name_check','transfer_individual_check','age76_individual_check','mixed_age_missing_certificate','internal_document_unknown','necessary_task_repeated_wording'}:
        decision.action='handoff'
        decision.handoff_reason='knowledge_confirmation_required'
    elif name=='valid_missing_material':
        decision.action='handoff'
        decision.handoff_reason='requested_material_unavailable'
        decision.v2_events=[{'type':'material_requested','quote':question,'material_kind':'altitude'}]
    elif name=='valid_pending_guide':
        decision.action='handoff'
        decision.handoff_reason='lead_captured'
        decision.lead_action='captured'
        decision.contact_values={'wechat':'test_traveller26'}
        decision.safety_flags=['pending_material:altitude_guide']
        decision.v2_events=[{'type':'human_requested','quote':question}]
    context={'engine_version':'v2','module':'reply','customer_text':question,
        'route_variant':'peach_9d_2027','available_materials':[]}
    if name.startswith('contact_appointment_'):
        context['now']='2026-09-20T02:00:00+00:00'
        decision.action='handoff'
        decision.handoff_reason='customer_contact_outside_window'
        decision.v2_events=[] if name.endswith('no_event') else [{'type':'contact_agreed','quote':question,'contact_at':'2026-09-22T10:00:00+08:00'}]
    if name.startswith('permission_'):
        decision.slots={'departure_window':'未定'}
        decision.slot_evidence={'departure_window':'出發日還沒定'}
        decision.v2_events=[{'type':'profile_updated','quote':'出發日還沒定'}]
        if name=='permission_only_plain':
            decision.v2_events.append({'type':'question','quote':question})
        else:
            decision.material_keys=['routes12-9d-itinerary']
            decision.v2_events.append({'type':'material_requested','quote':question,'material_kind':'itinerary'})
    if name.startswith('caption_'):
        decision.material_keys=['routes12-vehicle','routes12-vehicle-oxygen']
        decision.v2_events=[{'type':'material_requested','quote':question,'material_kind':'vehicle'},
                            {'type':'question','quote':question,'topic':'tips'}]
        decision.v2_delivery_sections=[{'group_key':'vehicle_reference','text':body,'asset_keys':[],
            'evidence_refs':refs,'delivery_mode':'text_only','answers_customer_question':True},
            {'group_key':'vehicle_reference','text':'4至6人用車安排2025年9座VIP航空座椅車，配有彌散式供氧及緊急醫療氧氣鋼瓶。這是車內與供氧設備的照片。',
             'asset_keys':decision.material_keys,'evidence_refs':['route.shared.vehicle_reference'],'delivery_mode':'assets_then_text'}]
    if name=='return_rail_ellipsis_unasked_fee':
        context['context_messages']=[{'role':'customer','content':'可以坐青鐵入藏嗎？'},
            {'role':'assistant','content':'這條林芝進、拉薩出，青鐵入藏不是行程安排；可考慮回程从拉薩搭鐵路出藏。'}]
    if name.startswith('group8_invented_'):
        decision.slots={'party_size':'8'}
        decision.slot_evidence={'party_size':'我們8人'}
        decision.action='handoff'
        decision.handoff_reason='large_group_custom_quote'
        decision.v2_events.append({'type':'profile_updated','quote':'我們8人'})
    if name.startswith('rongbuk_') or name=='hotel_contradictory_bomi_quantifier':
        decision.route_variant=context['route_variant']='peach_11d_2027'
        decision.branch='peach_11d'
    if name in {'valid_price','price_scope'}:
        decision.slots={'party_size':'6'}
        decision.slot_evidence={'party_size':'6人'}
    if name.startswith('hypothetical_party_'):
        context['journey']={'customer_profile':{'party_size':'2'}}
        if name.endswith('_stored'):
            decision.slots={'party_size':'6'}
            decision.slot_evidence={'party_size':'6人'}
    if name in {'missing_date_storage','valid_date_storage'}:
        decision.v2_events=[{'type':'profile_updated','quote':question}]
        decision.slots={'party_size':'6'}
        decision.slot_evidence={'party_size':'6位'}
        context['journey']={'customer_profile':{'departure_window':'3月25日'}}
        if name=='valid_date_storage':
            decision.slots['departure_window']='3月28日'
            decision.slot_evidence['departure_window']='3月28日'
    if name=='hotel_requested_repeat':
        context['context_messages']=[{'role':'user','content':'想去9日'},{'role':'assistant','content':'9日全程供氧住宿，波密是希爾頓升級的例外。'}]
    if name=='wechat_purpose_after_refusal':
        context['context_messages']=[{'role':'user','content':'不想留LINE，在哪集合？'},{'role':'assistant','content':'集合地點是林芝。'},{'role':'user','content':'入藏函在哪拿？'},{'role':'assistant','content':'成都由專人交付。'}]
    if name.startswith('oxygen_') and name not in {'oxygen_guarantee'}:
        context['global_knowledge_facts']=[{'id':'web.700.1.7.0',
            'text':'前往5000公尺以上景點的供氧安排：車上備有氧氣鋼瓶，並提供每人一支隨身氧氣瓶。這項說明不代表所有路線與所有區段均適用，具體供應及租用費用需依行程確認，也不構成醫療效果保證。'}]
    if name.startswith('proactive_hotel_'):
        context.update(module='silence_touch',route_variant='peach_11d_2027',context_messages=[{'role':'assistant','content':'珠峰段住絨布旅館，房內有供氧和獨立衛浴。'}])
        context['v2_proactive_candidate_fact_ids']=['route.shared.hotel_reference']
        context['v2_delivered_fact_ids']=['route.11.rongbuk']
        decision.route_variant='peach_11d_2027'
        decision.branch='peach_11d'
        decision.v2_events=[]
    if name.startswith('flight_'):
        context['global_knowledge_facts']=[{'id':'web.flight.service','text':'顧問可依需求提供航班建議並協助代訂，實際票價需依出發地與日期核對。'}]
    from app.decision_knowledge import FACTS
    from app.release_provenance import source_fingerprint, assert_source_unchanged
    fingerprint=source_fingerprint()
    context['v2_available_fact_ids']=[f['id'] for f in FACTS
        if not f.get('branches') or decision.branch in f['branches']]
    context['v2_available_fact_ids'] += [f['id'] for f in context.get('global_knowledge_facts',[])]
    try:
        result, logs, digest = _verify(context,decision)
    except Exception as exc:
        folder=Path(os.environ.get('V2_VERIFIER_OUTPUT','../output/v2-completion-20260920/verifier-controls'))
        folder.mkdir(parents=True,exist_ok=True)
        (folder/f'{name}-{repeat+1}.json').write_text(json.dumps({'release':ENGINE_RELEASE_ID,
            'question':question,'body':body,'expected':expected,'accepted':None,'pass':False,
            'error':str(exc),'calls':getattr(exc,'logs',[]),'real_customer_messages':0,
            'source_fingerprint':fingerprint,'source_unchanged':source_fingerprint()==fingerprint},
            ensure_ascii=False,indent=2),encoding='utf8')
        raise

    accepted = result.supported and result.relevant and not result.contract_violations
    folder=Path(os.environ.get('V2_VERIFIER_OUTPUT','../output/v2-completion-20260920/verifier-controls'))
    folder.mkdir(parents=True,exist_ok=True)
    (folder/f'{name}-{repeat+1}.json').write_text(json.dumps({'release':ENGINE_RELEASE_ID,
        'question':question,'body':body,'expected':expected,'accepted':accepted,'pass':accepted==expected,
        'audit':asdict(result),'calls':logs,'digest':digest,'real_customer_messages':0,
        'source_fingerprint':fingerprint,'source_unchanged':source_fingerprint()==fingerprint},ensure_ascii=False,indent=2),encoding='utf8')
    assert_source_unchanged(fingerprint)
    assert accepted == expected, asdict(result)
    if name in {'group8_invented_private','group8_invented_limit','hotel_double_exception',
                'hotel_invented_grade','discount_party_eligibility','guide_invented_drug_supply_exclusion',
                'price_missing_starting_condition','seasonal_company_invented_absence','oxygen_lost_condition'}:
        assert not result.supported, 'The factual defect itself must be rejected, not only another scope defect.'
    if name=='health_invented_aviation':
        assert any('適航評估' in v for v in result.contract_violations)
    if name=='internal_route_approval':
        assert any('内部资料核验' in v for v in result.contract_violations)
