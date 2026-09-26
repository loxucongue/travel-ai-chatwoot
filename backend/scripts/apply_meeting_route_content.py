"""Apply the 2026-09-07 route-content meeting decisions to packaged routes."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "data" / "knowledge" / "china2go" / "route-packages"
RUNTIME_ROOT = ROOT / "backend" / "data" / "route-packages"
SOURCE_REF = {
    "peach_9d_2027": "website-7693-full/02-peach-9d/content.md#介紹模式腳本",
    "peach_11d_2027": "website-7693-full/01-peach-everest-11d/content.md#介紹模式腳本",
}
ROUTE_MATCH_KEYWORDS = {
    "peach_9d_2027": [
        "9天行程", "九天行程", "不去珠峰", "不上珠峰", "輕鬆桃花行程", "轻松桃花行程",
    ],
    "peach_11d_2027": [
        "11天行程", "十一天行程", "珠峰", "珠峰大本營", "珠峰大本营", "絨布旅館", "绒布旅馆",
    ],
}
INITIAL_GROUPS = {
    "peach_9d_2027": {
        "itinerary_overview", "peach_highlights", "hotel_reference", "vehicle_reference",
    },
    "peach_11d_2027": {
        "itinerary_overview", "rongbuk_reference", "peach_highlights",
        "hotel_reference", "vehicle_reference",
    },
}
CONTENT_SEQUENCES = {
    "peach_9d_2027": [
        "itinerary_overview", "peach_highlights", "hotel_reference", "vehicle_reference",
        "accommodation_summary", "landmarks", "zhaji", "read_check",
    ],
    "peach_11d_2027": [
        "itinerary_overview", "rongbuk_reference", "peach_highlights", "hotel_reference",
        "vehicle_reference", "accommodation_summary", "landmarks", "zhaji", "read_check",
    ],
}
MAINLINE_TEXTS = {
    "peach_highlights": (
        "桃花沿線的景色也一起給您看～除了嘎拉桃花村、波密桃花溝，行程還會走帕邦喀寺和秀巴古堡，"
        "賞花之外也能看到寺院、古堡和沿線人文。"
    ),
    "hotel_reference": (
        "住宿這一組是希爾頓客房和房內設備～房間空間、床鋪、休息區和供氧設備都拍得很清楚，"
        "您可以直接看看實際環境。"
    ),
    "vehicle_reference": (
        "接著是行程用車～4至6人小團使用9座VIP航空座椅車，這組能看到座椅排列、"
        "車內空間和供氧設備，多日路程會搭什麼車一看就清楚。"
    ),
    "rongbuk_reference": (
        "這張是珠峰段入住的絨布旅館～客房裡有獨立衛浴和供氧設備，珠峰當晚住什麼環境，"
        "您可以先看得清清楚楚。"
    ),
    "accommodation_summary": (
        "再給您一張不同角度的住宿照片～房間整體和活動空間都看得到，也很方便轉給同行家人一起看。"
    ),
    "zhaji": (
        "行程裡也會去扎基寺～除了布達拉宮和八廓街，還能看看更貼近當地生活的寺院人文。"
    ),
    "party_question": "這次大概會有幾位一起來呢？",
    "departure_question": "您大概想安排在什麼時候呢？還沒決定也沒關係。",
    "contact_transition": (
        "完整行程和費用我都可以整理給您，之後時間確定或想到其他問題，也能直接接著聊。"
    ),
    "price_deferral": (
        "價格我會連同住宿、用車和包含項目一起說清楚，您比較起來會容易很多。"
    ),
}

ROUTE_MAINLINE_TEXTS = {
    "peach_9d_2027": {
        "entry_question": (
            "9日這條不走珠峰，時間主要放在林芝桃花和西藏經典景點～完整行程圖就在下面，"
            "每天怎麼走都標清楚了。"
        ),
        "party_intro_solo": (
            "一位也沒問題呀～行程、住宿和用車都可以先看看，等時間比較有想法時再接著安排就好。"
        ),
        "party_intro_small": (
            "2至3位可以先看這條4至6人小團～行程、桃花亮點、住宿和用車都整理好了，慢慢比較就可以。"
        ),
        "party_intro_group": (
            "人數比較多的安排會更有彈性～9日行程和實際配置先給您看，團體費用會再按人數另外規劃。"
        ),
        "itinerary_overview": (
            "9日這條從林芝開始，會走波密、拉薩、山南和日喀則，全程不走珠峰～"
            "完整行程圖放在這裡，每天的路線和主要停留點都標清楚了。"
        ),
        "landmarks": (
            "拉薩段也有布達拉宮和八廓街～所以9日行程不只賞桃花，經典地標和老城人文也會走到。"
        ),
        "read_check": (
            "您還在比較的話，可以先記住9日最直接的差別：不安排珠峰，時間會放在桃花和西藏經典景點。"
        ),
        "departure_reference": (
            "桃花線出發區間是3月20日至4月10日，每週五、六、日、一出發。日期有想法時再跟我說就好。"
        ),
    },
    "peach_11d_2027": {
        "entry_question": (
            "11日除了林芝桃花，後段還會到珠峰大本營～完整行程圖就在下面，桃花段和珠峰段怎麼接都看得到。"
        ),
        "party_intro_solo": (
            "一位也可以先了解呀～行程、珠峰住宿和用車都可以先看看，等時間比較有想法時再接著安排就好。"
        ),
        "party_intro_small": (
            "2至3位可以先看這條小團行程～珠峰段住宿、桃花亮點和用車都整理好了，慢慢比較就可以。"
        ),
        "party_intro_group": (
            "人數比較多的安排會更有彈性～11日行程和實際配置先給您看，團體費用會再按人數另外規劃。"
        ),
        "itinerary_overview": (
            "11日從林芝開始，會走波密、拉薩、山南和日喀則，後段再到珠峰大本營～"
            "完整行程圖放在這裡，每天怎麼走、哪一段上珠峰都標清楚了。"
        ),
        "landmarks": (
            "拉薩段也有布達拉宮和八廓街～11日除了桃花和珠峰，經典地標與老城人文也會安排進去。"
        ),
        "read_check": (
            "您還在比較的話，可以先記住11日最直接的差別：桃花行程之外還會到珠峰，也安排了珠峰段住宿。"
        ),
        "departure_reference": (
            "桃花加珠峰線出發區間是3月20日至4月10日，每週五、六、日、一出發。日期有想法時再跟我說就好。"
        ),
    },
}


COMMON_WEBSITE_ANSWERS = {
    "peach_highlights": (
        "這個行程除了必去的兩大桃花景點－嘎拉桃花村與波密桃花溝外，還有加碼去到以雪山湖泊、王宮遺址、千年古堡、以及最重要的拉薩秘境寺廟的特殊桃花行程\n"
        "讓桃花能加入更深的西藏色彩，讓你不是只有賞桃花而已，而是真正的擁有了西藏獨有的桃花特色"
    ),
    "hotel": (
        "以及除了地區條件有限以外，我們全面升級都住國際品牌希爾頓飯店哦！"
    ),
    "vehicle": (
        "因為桃花節不加價升級6小團！車型則是2025年最新車，用VIP航空母車，每個人都有自己的大座位，並且有自帶製氧機，讓您舒適又安心～💕\n"
        "然後~這台車除了前面有緊急醫療氧氣鋼瓶！還特別裝了大家都可以吸的彌散式氧氣，簡單說就是～只要一開車，車子內部就在一個【移動氧艙】"
    ),
    "departure": "出發時間：3/20-4/10（每週五、六、日、一發團）",
    "documents": "包含：入藏函，車，導遊，司機，保險，住宿，酒店早餐，門票",
    "solo_guest": "您好~我們很多自己一人的旅客，非常歡迎您！，先給您看一下我們的行程",
    "small_party": "歡迎，我是China2Go國旅環球，我們的拚團都是4-6人小團，最適合像您們人數少前往的，比較自在！先給您介紹一下行程",
    "group_party": (
        "我們公司是全西藏精緻中小團資源最多的！我們的拚團都是4-6人一團~\n"
        "你們可以考慮自己一團出發喔~\n"
        "不過先讓我先介紹一下行程"
    ),
    "lhasa_landmarks": "給您介紹一下裡面的行程．除了桃花秘境之外，我們也安排了經典的布達拉宮與大昭寺八廓街～",
    "zhaji_temple": (
        "以及我們家因為走的是純玩小團之深度行程，所以是親自帶大家去財神廟-扎基寺參觀！\n"
        "這裡是西藏最靈驗，香火非常鼎盛的廟喔！"
    ),
    "more_accommodation": "在西藏這樣的高原環境，住宿與車配備好！真的很重要",
    "no_shopping_contract_claim": "還有一點，我們是唯一敢把無購物寫在合約上的旅行社，有任何購物行為賠償5000！",
    "senior_health_document": (
        "是這樣的～目前用台胞證進入西藏的旅客，65歲以上都是要辦理健康證明，不過放心～我到時候幫您備註下～"
        "健康證明的格式還有申請規定，我再發給您！也都會依照時間提醒您辦理"
    ),
    "age_75_entry_claim": "目前75歲以上長輩申請入藏函是申請不下來的",
    "young_child_suitability": "我們通常是建議孩子5歲以上，能準確表達自己的身體狀況才會建議前往喔！",
    "ticket_refund_claim": "只要符合景區當下政策我們都會退優惠門票喔！",
    "hotel_oxygen_concentration_claim": "希爾頓是西藏最穩定且是12小時提供85-90%濃度的氧氣呦！",
    "mobile_oxygen_cabin_claim": (
        "然後~這台車除了前面有緊急醫療氧氣鋼瓶！還特別裝了大家都可以吸的彌散式氧氣，"
        "簡單說就是～只要一開車，車子內部就在一個【移動氧艙】"
    ),
    "vehicle_model_year_claim": "車型則是2025年最新車，用VIP航空母車，每個人都有自己的大座位，並且有自帶製氧機，讓您舒適又安心～💕",
}

# These are preserved verbatim from the website, but they contain live,
# medical-effect, equipment-year, or contractual claims that require an
# explicit business review before they can be sent to a customer.
WEBSITE_PENDING_REVIEW_IDS = {
    "vehicle",
    "no_shopping_contract_claim",
    "senior_health_document",
    "age_75_entry_claim",
    "young_child_suitability",
    "ticket_refund_claim",
    "hotel_oxygen_concentration_claim",
    "mobile_oxygen_cabin_claim",
    "vehicle_model_year_claim",
}

WEBSITE_ANSWERS = {
    "peach_9d_2027": {
        **COMMON_WEBSITE_ANSWERS,
        "peach_highlights": "這個行程除了必去的兩大桃花景點－嘎拉桃花村與波密桃花溝外，還有加碼去到以雪山湖泊、王宮遺址、千年古堡、以及最重要的拉薩秘境寺廟的特殊桃花行程",
        "route_overview": (
            "這是我們精選9日的桃花節行程！從林芝低海拔入藏，循序漸進的海拔可以給身體時間適應高原～\n"
            "而行程完整涵蓋【林芝+波密＋拉薩+山南+日喀則】等最重要的旅遊景點！"
        ),
        "price": (
            "【西藏桃花節9日遊（不上珠峰）】\n"
            "出發時間：3/20-4/10（每週五、六、日、一發團）\n"
            "十人小團優惠價：¥9,980/人（客人為2人1標間拼住價格，如需單休須補單房差¥2,600）\n"
            "★限量升級4-6人小團 價格不變\n"
            "包含：入藏函，車，導遊，司機，保險，住宿，酒店早餐，門票\n"
            "不含：機票、正餐、小費"
        ),
    },
    "peach_11d_2027": {
        **COMMON_WEBSITE_ANSWERS,
        "route_overview": (
            "這是我們精選11日的行程！從林芝低海拔入藏，循序漸進的海拔可以給身體時間適應高原～\n"
            "而行程完整涵蓋【林芝+波密＋拉薩+山南+日喀則以及珠峰大本營】等最重要的旅遊景點！"
        ),
        "price": (
            "【西藏桃花節11日遊】\n"
            "出發時間：3/20-4/10（每週五、六、日、一發團）\n"
            "十人小團優惠價：¥11,480/人（客人為2人1標間拼住價格，如需單休須補單房差¥3,200）\n"
            "★限量升級4-6人小團 價格不變\n"
            "包含：入藏函，車，導遊，司機，保險，住宿，酒店早餐，門票\n"
            "不含：機票、正餐、小費"
        ),
        "rongbuk_hotel": (
            "而您選的是我們限量上珠峰的桃花節行程．這是獨有＂升級＂在珠峰絨布旅館的住宿！不過因為房間數較少，人數也是有限定的喔！\n"
            "這是絨布旅館的住宿環境，有獨立廁所以及供氧，不過真的是限量喔！！！！"
        ),
    },
}

WEBSITE_SOURCE_LINES = {
    "peach_9d_2027": {
        "route_overview": "L43-L44", "peach_highlights": "L48", "hotel": "L56",
        "vehicle": "L64-L70", "price": "L166-L172", "departure": "L168", "documents": "L171",
        "solo_guest": "L29", "small_party": "L33", "group_party": "L23-L25",
        "lhasa_landmarks": "L92", "zhaji_temple": "L98-L102", "more_accommodation": "L76",
        "no_shopping_contract_claim": "L82", "senior_health_document": "L124",
        "age_75_entry_claim": "L124", "young_child_suitability": "L126", "ticket_refund_claim": "L122",
        "hotel_oxygen_concentration_claim": "L56", "mobile_oxygen_cabin_claim": "L70",
        "vehicle_model_year_claim": "L64",
    },
    "peach_11d_2027": {
        "route_overview": "L44-L45", "peach_highlights": "L59-L63", "hotel": "L71",
        "vehicle": "L79-L85", "price": "L181-L187", "departure": "L183", "documents": "L186",
        "solo_guest": "L30", "small_party": "L34", "group_party": "L24-L26",
        "lhasa_landmarks": "L107", "zhaji_temple": "L113-L117", "more_accommodation": "L91",
        "no_shopping_contract_claim": "L97", "senior_health_document": "L139",
        "age_75_entry_claim": "L139", "young_child_suitability": "L141", "ticket_refund_claim": "L137",
        "hotel_oxygen_concentration_claim": "L71", "mobile_oxygen_cabin_claim": "L85",
        "vehicle_model_year_claim": "L79", "rongbuk_hotel": "L49-L55",
    },
}


def answer(
    package: dict,
    answer_id: str,
    name: str,
    topics: list[str],
    group_key: str,
    text: str,
    examples: list[str],
    source_text: str,
    *,
    priority: int = 100,
    assets: list[str] | None = None,
    negative: list[str] | None = None,
    party_min: int | None = None,
    party_max: int | None = None,
    status: str = "active",
) -> dict:
    group = package["content_groups"][group_key]
    route = package["route_variant"]
    website_text = WEBSITE_ANSWERS.get(route, {}).get(answer_id)
    source_line = WEBSITE_SOURCE_LINES.get(route, {}).get(answer_id, "介紹模式腳本")
    effective_status = "pending_review" if answer_id in WEBSITE_PENDING_REVIEW_IDS else status
    return {
        "id": answer_id,
        "name": name,
        "status": effective_status,
        "priority": priority,
        "topics": topics,
        "party_size_min": party_min,
        "party_size_max": party_max,
        "content_group_key": group_key,
        "answer_text": website_text or text,
        "fact_ids": list(group.get("evidence_refs") or []),
        "asset_ids": list(group.get("asset_keys") or []) if assets is None else assets,
        "source_ref": f"{SOURCE_REF[route].split('#', 1)[0]}#{source_line}",
        "answer_origin": "website_verbatim" if website_text else "operator_approved",
        "positive_examples": examples,
        "negative_examples": negative or [],
    }


def fixed_answers(package: dict) -> list[dict]:
    route = package["route_variant"]
    days = "9" if route == "peach_9d_2027" else "11"
    result = [
        answer(
            package, "route_overview", "行程怎麼走", ["route_intro", "itinerary"],
            "itinerary_overview",
            (
                "我先把9日行程圖發您看～這條從林芝開始，會走波密、拉薩、山南和日喀則，"
                "全程不安排珠峰；每天的停留點都標在圖裡，您和同行者一起看會很清楚。"
                if days == "9" else
                "我先把11日行程圖發您看～這條從林芝開始，會走波密、拉薩、山南和日喀則，"
                "後段再前往珠峰大本營；每天的停留點都標在圖裡，您和同行者一起看會很清楚。"
            ),
            [f"{days}日行程怎麼走", f"想看{days}日行程", "每天會去哪裡"],
            (
                f"這是我們精選{days}日的桃花節行程！從林芝低海拔入藏，循序漸進的海拔可以給身體時間適應高原～"
            ),
            priority=130,
            negative=["多少錢", "住哪裡", "用什麼車"],
        ),
        answer(
            package, "peach_highlights", "桃花與沿線亮點", ["highlights"],
            "peach_highlights",
            "這組是桃花沿線的帕邦喀寺和秀巴古堡～除了嘎拉桃花村、波密桃花溝，行程也會穿插寺院和古堡，賞花之外還能看到不同的西藏人文與沿線景觀。",
            ["有什麼亮點", "桃花景點有哪些", "除了桃花還看什麼"],
            "這個行程除了必去的兩大桃花景點－嘎拉桃花村與波密桃花溝外，還有雪山湖泊、王宮遺址、千年古堡與拉薩秘境寺廟。",
            priority=120,
        ),
        answer(
            package, "hotel", "飯店與房內設備", ["hotel"],
            "hotel_reference",
            "我先把希爾頓客房和房內設備照片發您看～照片裡可以直接看到房間、床鋪和供氧設備；實際入住飯店與房型會依團期安排確認。",
            ["住什麼飯店", "住宿環境怎麼樣", "房間有供氧嗎", "可以看房間照片嗎"],
            "除了地區條件有限以外，我們全面升級都住國際品牌希爾頓飯店；網頁另列希爾頓客房與製氧機圖片。",
            priority=120,
            negative=["珠峰住哪裡", "絨布旅館"],
        ),
        answer(
            package, "vehicle", "用車與車內設備", ["vehicle"],
            "vehicle_reference",
            "我把實際用車照片發您看～4至6人小團使用9座VIP航空座椅車，照片裡能看到獨立座椅、車內空間和供氧設備，多日行程會搭什麼車可以先看得很清楚。",
            ["坐什麼車", "車上有氧氣嗎", "用車舒服嗎", "可以看車子照片嗎"],
            "車型使用VIP航空座椅車，每個人有獨立座位；網頁另列車輛與車載製氧機圖片。",
            priority=120,
        ),
        answer(
            package, "price", "費用與包含項目", ["price"],
            "price_reference", package["content_groups"]["price_reference"]["approved_text"],
            ["一個人多少錢", "團費多少", "價格包含什麼", "單房差多少"],
            "客人問價格：價格、多少錢、一人多少、優惠、價錢、團費、行程費用等，使用網站對應行程價格內容。",
            priority=150,
            assets=[],
            negative=[
                "還有名額嗎", "還有位子嗎", "今天還有位子嗎",
                "現在幾個人報名", "一定會出團嗎",
            ],
        ),
        answer(
            package, "departure", "出發日期", ["departure"],
            "departure_reference",
            "桃花行程目前的出發區間是3月20日至4月10日，每週五、六、日、一出發。日期確定後，China2Go這邊再替您核對當天團況。",
            ["幾月出發", "有哪些出發日", "三月底可以嗎", "四月有團嗎"],
            "這路線是3月尾至4月出，每週4個出團日。",
            priority=130,
            assets=[],
            negative=[
                "還有名額嗎", "還有位子嗎", "今天還有位子嗎",
                "現在幾個人報名", "一定會出團嗎",
            ],
        ),
        answer(
            package, "documents", "入藏函與資料", ["documents"],
            "price_reference",
            "入藏函已包含在行程費用內；實際需要準備的資料會依旅客身分與當時規定確認，China2Go這邊會再由旅遊顧問替您整理。",
            ["入藏函有包含嗎", "需要辦什麼證件", "台灣旅客要準備什麼"],
            "網站價格內容列明包含入藏函；長者健康證明與年齡規定需依當時政策另外核對。",
            priority=140,
            assets=[],
        ),
        answer(
            package, "solo_guest", "一位旅客", ["party_size"],
            "party_intro_solo",
            "一位也可以先了解呀～我們有不少自己出發的旅客，我先把行程、住宿和用車整理給您看，之後再依您確定的時間接著安排。",
            ["我一個人可以嗎", "我一個人可以參加", "只有一位能參加嗎", "一人能報名嗎"],
            "我們很多自己一人的旅客，非常歡迎您！先給您看一下我們的行程。",
            priority=120,
            assets=[],
            party_min=1,
            party_max=1,
        ),
        answer(
            package, "small_party", "2至3位旅客", ["party_size"],
            "party_intro_small",
            "2至3位很適合先看4至6人的精緻小團～我先把行程、住宿和用車整理給您，幾個人同行都能一起比較。",
            ["我們兩位可以嗎", "三個人能參加嗎", "2到3人怎麼安排"],
            "我們的拼團是4至6人小團，很適合人數較少的旅客，行程比較自在。",
            priority=120,
            assets=[],
            party_min=2,
            party_max=3,
        ),
        answer(
            package, "group_party", "4至7位旅客", ["party_size"],
            "party_intro_group",
            "4至7位可以先看這條精緻小團行程～我先把行程、住宿和用車整理給您；如果想自己一團出發，也可以再由旅遊顧問依實際人數安排。",
            ["我們4位可以嗎", "我們6個人怎麼安排", "7位可以參加嗎"],
            "4-6人／6人以上分支先介紹精緻小團；客戶可考慮自己一團出發。",
            priority=125,
            assets=[],
            party_min=4,
            party_max=7,
        ),
        answer(
            package, "lhasa_landmarks", "布達拉宮與八廓街", ["highlights"],
            "landmarks",
            "除了桃花景點，行程也安排布達拉宮和八廓街～我把照片發您看，拉薩經典地標和老城人文都能一起看到。",
            ["有布達拉宮嗎", "會去八廓街嗎", "拉薩有哪些景點"],
            "除了桃花秘境之外，行程也安排經典的布達拉宮與大昭寺八廓街。",
            priority=145,
        ),
        answer(
            package, "zhaji_temple", "扎基寺", ["highlights"],
            "zhaji",
            "行程也會安排扎基寺～我把照片發您看，除了布達拉宮和八廓街，也能看到更有當地生活感的寺院人文。",
            ["會去扎基寺嗎", "有財神廟嗎", "還有哪些寺廟"],
            "純玩小團深度行程安排扎基寺參觀。",
            priority=150,
        ),
        answer(
            package, "more_accommodation", "其他住宿照片", ["hotel"],
            "accommodation_summary",
            "我再把另一個角度的住宿照片發您看～這張可以看到房間整體和活動空間，也方便您直接轉給同行者一起看。",
            ["還有其他住宿照片嗎", "可以再看住宿照片嗎", "想多看一張房間"],
            "介紹模式圖13為住宿第二張，後續可補充另一角度的住宿環境。",
            priority=155,
        ),
        answer(
            package, "no_shopping_contract_claim", "無購物合約承諾", ["other"],
            "contact_transition",
            "無購物與相關賠償內容需要依正式合約條款核對，這則固定回答目前不對客戶啟用。",
            ["有購物嗎", "會帶去購物站嗎", "無購物會寫進合約嗎"],
            "我們是唯一敢把無購物寫在合約上的旅行社，有任何購物行為賠償5000！",
            priority=200,
            assets=[],
            status="pending_review",
        ),
        answer(
            package, "senior_health_document", "65歲以上健康文件", ["documents", "altitude"],
            "contact_transition",
            "65歲以上旅客的健康文件與申請規定，需要依出發時的政策和個人情況由旅遊顧問確認。",
            ["65歲以上要健康證明嗎", "長輩去西藏要準備什麼", "老人家可以辦入藏函嗎"],
            "網站腳本記載：65歲以上台胞證旅客需要辦理健康證明，格式與申請規定另行提供。",
            priority=180,
            assets=[],
            status="pending_review",
        ),
        answer(
            package, "age_75_entry_claim", "75歲以上入藏函限制", ["documents"],
            "contact_transition",
            "75歲以上旅客的入藏函資格屬於個案規定，需要由旅遊顧問依出發時政策另外確認。",
            ["75歲可以去嗎", "75歲能辦入藏函嗎", "老人有年齡限制嗎"],
            "網站腳本記載：目前75歲以上長輩申請入藏函是申請不下來的。",
            priority=190,
            assets=[],
            status="pending_review",
        ),
        answer(
            package, "young_child_suitability", "5歲以下孩童", ["altitude"],
            "contact_transition",
            "孩童是否適合前往高原，需要依年齡和個人健康狀況另外評估，旅遊顧問可以先協助整理行程條件。",
            ["5歲小孩可以去嗎", "幼兒適合西藏嗎", "小朋友有年齡限制嗎"],
            "網站腳本建議孩童5歲以上、能準確表達身體狀況後再前往。",
            priority=180,
            assets=[],
            status="pending_review",
        ),
        answer(
            package, "ticket_refund_claim", "優惠門票退費", ["other"],
            "contact_transition",
            "長輩或孩童的優惠門票與退費方式，需要依景區當時政策和正式行程條款確認。",
            ["老人票會退嗎", "小孩門票有優惠嗎", "優惠門票怎麼退"],
            "網站腳本記載：符合景區當下政策會退優惠門票。",
            priority=170,
            assets=[],
            status="pending_review",
        ),
        answer(
            package, "hotel_oxygen_concentration_claim", "飯店供氧濃度", ["hotel", "altitude"],
            "hotel_reference",
            "飯店房內配置供氧設備；設備濃度、開放時段和實際使用條件需要依入住飯店確認。",
            ["飯店氧氣濃度多少", "飯店全天供氧嗎", "房間供氧幾小時"],
            "網站腳本宣稱希爾頓12小時提供85-90%濃度氧氣。",
            priority=180,
            assets=[],
            status="pending_review",
        ),
        answer(
            package, "mobile_oxygen_cabin_claim", "車內移動氧艙", ["vehicle", "altitude"],
            "vehicle_reference",
            "車內有彌散式供氧設備和緊急氧氣配置；設備不能當作醫療治療或安全保證。",
            ["車上是移動氧艙嗎", "車內氧氣有什麼效果", "車上有醫療氧氣嗎"],
            "網站腳本將車內彌散式供氧描述為移動氧艙，並提到緊急醫療氧氣鋼瓶。",
            priority=180,
            assets=[],
            status="pending_review",
        ),
        answer(
            package, "vehicle_model_year_claim", "車輛年份", ["vehicle"],
            "vehicle_reference",
            "行程使用VIP航空座椅車；實際車輛年份與車型需要依出發團期確認。",
            ["是2025新車嗎", "車子是哪一年的", "一定是最新車嗎"],
            "網站腳本宣稱使用2025年最新車。",
            priority=170,
            assets=[],
            status="pending_review",
        ),
    ]
    if route == "peach_11d_2027":
        result.append(answer(
            package, "rongbuk_hotel", "珠峰絨布旅館", ["rongbuk", "hotel"],
            "rongbuk_reference",
            "11日行程在珠峰段安排絨布旅館，我把房間照片發您看～照片裡可以看到客房、獨立衛浴和供氧設備，不是帳篷住宿。",
            ["珠峰住哪裡", "絨布旅館有衛浴嗎", "珠峰晚上住帳篷嗎"],
            "11日分支升級珠峰絨布旅館；住宿環境有獨立廁所以及供氧。",
            priority=160,
        ))
    return result


def update_package(path: Path) -> None:
    package = json.loads(path.read_text(encoding="utf-8"))
    route = package.get("route_variant")
    if route not in INITIAL_GROUPS:
        return
    replacements = {
        "專項顧問": "專人旅遊顧問",
        "線路": "行程",
        "酒店": "飯店",
        "聯繫方式": "聯絡方式",
    }
    for group_key, group in package["content_groups"].items():
        group["initial_delivery"] = group_key in INITIAL_GROUPS[route]
        text = str(group.get("approved_text") or "")
        for old, new in replacements.items():
            text = text.replace(old, new)
        group["approved_text"] = ROUTE_MAINLINE_TEXTS[route].get(
            group_key, MAINLINE_TEXTS.get(group_key, text)
        )
    package["content_sequence"] = CONTENT_SEQUENCES[route]
    package["match_keywords"] = ROUTE_MATCH_KEYWORDS[route]
    for node in package["sop"]["nodes"]:
        for message in node.get("messages") or []:
            if message.get("content_type", "text") != "text":
                continue
            text = str(message.get("content") or "")
            for old, new in replacements.items():
                text = text.replace(old, new)
            group_key = str(node.get("content_group_key") or "")
            message["content"] = ROUTE_MAINLINE_TEXTS[route].get(
                group_key, MAINLINE_TEXTS.get(group_key, text)
            )
    package["fixed_answers"] = fixed_answers(package)
    package["package_version"] = "2026-09-08.taiwan-voice-1"
    path.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    paths = [
        SOURCE_ROOT / "peach-9d-2027" / "route-package.json",
        SOURCE_ROOT / "peach-11d-2027" / "route-package.json",
        RUNTIME_ROOT / "peach_9d_2027" / "route-package.json",
    ]
    for path in paths:
        if path.is_file():
            update_package(path)
            print(path)


if __name__ == "__main__":
    main()
