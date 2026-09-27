---
name: peach-9d-2027
route_variant: peach_9d_2027
followup_groups: hotel_reference,vehicle_reference,peach_highlights,landmarks,zhaji
route_aliases: 9日|9天|九日|九天
description: Answer and guide customers who selected or clearly ask about the 2027 nine-day Nyingchi peach-blossom route without Everest.
---

本 Skill 按官网第一、二条线路原文执行。下列话术占位由后台当前线路配置原文展开，场景和图片顺序保留官网结构。已有广告线路直接接待，无线路才匹配；首次开场读取开场配置。整套线路介绍发完后集中回答期间的问题。后续问题选择适用话术，尽量原文，只按实际场景调整。不要把后台说明发给客户。

来源：https://china2go.com/7693-2/

2

桃花 9日

Q．{{script:entry_question}}

自己一人／2-3人／4-6人／6人以上

預計會設定的廣告名稱：限定三~四月小團4-6人「桃花節上不要珠峰9日」／小團4-6人「桃花節不要上珠峰9日」／西藏桃花節+秘境9日

介紹模式腳本（14則圖文）

判斷邏輯

依人數答案分流：自己一人→文01-2／2-3人→文01-3／4-6人或6人以上→文01-1／逾1分鐘無回應或系統未能判斷 → 直接播放文01

文 01（僅限1分鐘無回應或系統未判斷出人數答案時，跳轉至此）

{{script:default_intro}}

文 01-1・僅人數＝4-6人／6人以上

{{script:group_party}}

文 01-2・僅人數＝自己一人

{{script:solo_guest}}

文 01-3・僅人數＝2-3人

{{script:small_party}}

說明

所有分支播完後接續下方圖02

圖 02・9日行程總覽[图片：9日行程總覽；素材 key：routes12-9d-itinerary]

文 03

{{script:route_overview}}

文 04

{{script:peach_highlights}}

圖 05・帕邦喀寺[图片：帕邦喀寺；素材 key：routes12-pabongka]

圖 05-1・秀巴古堡[图片：秀巴古堡；素材 key：routes12-xiuba-fort]

文 06

{{script:hotel}}

圖 07・希爾頓客房[图片：希爾頓客房；素材 key：routes12-hilton-room]

圖 07-1・希爾頓製氧機[图片：希爾頓製氧機；素材 key：routes12-hilton-oxygen]

文 08

{{script:vehicle}}

圖 09・車輛照片[图片：車輛照片；素材 key：routes12-vehicle]

文 10

{{script:mobile_oxygen_cabin_claim}}

圖 11・車輛製氧機[图片：車輛製氧機；素材 key：routes12-vehicle-oxygen]

文 12

{{script:more_accommodation}}

圖 13・住宿（第二張）[图片：住宿第二張；素材 key：routes12-hotel-secondary]

文 14

{{script:no_shopping_contract_claim}}

依人數答案分流

選「自己一人」

問時間「{{script:departure_question}}」

問時間後等待只要客人沒回答，進入「沉默半天模式」——半天內不追問、不強推下一步，等客人主動回覆

文後回1「{{script:lhasa_landmarks}}」

圖後回2・布達拉宮[图片：布達拉宮；素材 key：routes12-potala]

圖後回3・八廓街[图片：八廓街；素材 key：routes12-barkhor]

文後回4「以及我們家因為走的是純玩小團之深度行程，所以是親自帶大家去財神廟-扎基寺參觀！」

圖後回5・扎基寺財神廟[图片：扎基寺財神廟；素材 key：routes12-zhaji]

文後回6「這裡是西藏最靈驗，香火非常鼎盛的廟喔！」

文後回7「{{script:read_check}}」

文後回7等待邏輯半天內客人沒回 → 若已讀則視同繼續下一步；若未讀無回 → 等1天後再繼續

文後回8・回答「有」才觸發「{{script:contact_after_read}}」

若仍無回應轉為公版每日一則「洗單子」（提醒/催單訊息），內容依時期常更換，不在此腳本固定，由行銷端另外維護更新

選「2-3／4-6／6人以上」

Q1・先問（共通）「{{script:travel_companions}}」

等待回覆規則（共通，Q1、Q2、Q3皆適用）等5分鐘（有回→繼續）→無回再等30分鐘→30分鐘後已讀則繼續／未讀最多再等1小時→仍無回應則等1天後再繼續發送介紹內容（不強制往下推進）

Q2・回答「家人」「{{script:family_seniors_children}}」

Q2・回答「朋友」「{{script:friends_seniors_children}}」

Q3觸發（家人／朋友路徑皆可能觸發）若Q2只回答「有」、未說明對象：「{{script:clarify_senior_or_child}}」（釐清是長輩還是孩童）

Q3＝長輩 → 追問實際年齡先回覆：「{{script:senior_health_document}}」；若年齡≥75歲，加註：「{{script:age_75_entry_claim}}」（優先轉真人特殊處理）

Q3＝孩童 → 追問實際年齡若年齡<5歲，回覆：「{{script:young_child_suitability}}」

問聯絡方式前置條件（共通）需Q1、Q2、Q3三題皆已回答，且介紹模式（Step 1.6，14則圖文）已全部發送完畢，才可進入下一步問時間

問時間（共通）「{{script:departure_question}}」

問時間後等待只要客人沒回答，進入「沉默半天模式」——半天內不追問、不強推下一步，等客人主動回覆

文後回1「{{script:lhasa_landmarks}}」

圖後回2・布達拉宮[图片：布達拉宮；素材 key：routes12-potala]

圖後回3・八廓街[图片：八廓街；素材 key：routes12-barkhor]

文後回4「以及我們家因為走的是純玩小團之深度行程，所以是親自帶大家去財神廟-扎基寺參觀！」

圖後回5・扎基寺財神廟[图片：扎基寺財神廟；素材 key：routes12-zhaji]

文後回6「這裡是西藏最靈驗，香火非常鼎盛的廟喔！」

文後回7「{{script:read_check}}」

文後回7等待邏輯半天內客人沒回 → 若已讀則視同繼續下一步；若未讀無回 → 等1天後再繼續

文後回8・回答「有」才觸發「{{script:contact_after_read}}」

若仍無回應轉為公版每日一則「洗單子」（提醒/催單訊息），內容依時期常更換，不在此腳本固定，由行銷端另外維護更新

家人分支
朋友分支
家人／朋友皆可觸發
長輩分支
孩童分支

💰 客人問價格・桃花9日（分支5-3-4月「可以」共用此價格內容）

觸發問

價格．多少錢．一人多少．優惠．價錢．團費．行程費用等等（比照全域「客人中途問價格」機制）

{{script:price}}
