"""Frozen business scenarios: two independently worded inputs per requirement.

Assertions are authored before model runs; failures must not be silently relaxed.
Source: AI.docx, AI(1).docx, AI(2).docx and September 20 acceptance ledger.
"""
NINE, ELEVEN = 'peach_9d_2027', 'peach_11d_2027'
# id, requirement, category, two customer utterances, required alternative groups.
SPECS = [
 ('custom_requested','H01','handoff','請幫我另外規劃9月的西藏行程。|我們想九月去西藏，請安排客製路線並報價。',['顧問|顾问','規劃|规划|核對|核对|安排']),
 ('brand','B01','fact','你們是什麼公司？|China2Go的團型特色是什麼？', ['China2Go|國旅|国旅|小團|小团']),
 ('group','B01','fact','小團是多少人？|通常幾位旅客一起出發？', ['4|四','10|十']),
 ('shopping','B01','fact','會不會進購物店？|有安排購物站嗎？', ['不|無|无|零|沒有|没有']),
 ('nationality','B01','fact','大陸旅客可以一起拼團嗎？|你們會安排我們跟內地客人同團嗎？', ['不|無|无|外籍|境外']),
 ('price','B02','fact','6個人的9日價格多少？|桃花9天六人團每位多少錢？', ['9[,，]?980','6|六']),
 ('discount','B02','fact','多人一起報名有優惠嗎？|三對夫妻一起參加，能給多少折扣？', ['優惠|优惠|折|顧問|顾问']),
 ('room','B03','fact','房間是幾人住？|費用是以幾人一間房計算？', ['兩|两|2|雙|双']),
 ('hotel','B03','fact','全程都住希爾頓嗎？|住宿有什麼例外？波密也是希爾頓嗎？', ['波密']),
 ('rongbuk','B03','fact','珠峰那晚有獨立衛浴嗎？|絨布住宿的廁所和供氧怎樣？', ['獨立|独立','氧']),
 ('vehicle','B04','fact','用什麼車型？|四到六人使用什麼車？', ['2025','9|九','氧']),
 ('comfort','B04','fact','車上坐起來舒服嗎？|長途坐車會很擠嗎，有供氧嗎？', ['氧']),
 ('arrival','B05','fact','在哪裡集合？|第一天我要飛到哪個城市會合？', ['林芝']),
 ('not_chengdu','B05','fact','是在成都集合開始玩嗎？|是不是從成都出發一路玩進西藏？', ['林芝']),
 ('permit','B05','fact','入藏函在哪拿？|到成都怎麼拿到入藏函？', ['成都']),
 ('rail_in','B05','fact','可以搭青鐵入藏嗎？|我想坐青藏鐵路進西藏，這條包含嗎？', ['林芝|飛|飞','不|非|沒有|没有']),
 ('rail_out','B05','fact','回程想坐青藏鐵路可以嗎？|鐵路出藏車票包含在費用裡嗎？', ['另|不含|未含|不包含|額外|额外|核對|核对']),
 ('transfer','B05','handoff','不經成都，從重慶飛怎麼交函？|我從上海轉機，能照樣交入藏函嗎？', []),
 ('age64','B06','fact','台灣人64歲可以去嗎，最低65嗎？|我64歲是不是還不到參團年齡？', ['不|可以|能']),
 ('age65','B06','critical','台灣旅客65歲要健康證明嗎？|剛滿65歲的台灣人需要什麼證明？', ['健康證明|健康证明']),
 ('age75','B06','critical','台灣人剛滿75歲能參加嗎？|我媽媽台灣籍75歲，是否可以報名？', ['健康證明|健康证明']),
 ('age76','B06','critical','台灣人76歲適合去嗎？|爸爸76歲了，還能參團嗎？', ['不建議|不建议|超過|超过|建議不報名|建议不报名']),
 ('age_other','B06','handoff','香港人70歲一定要健康證明嗎？|我是馬來西亞籍68歲，證件規則一樣嗎？', []),
 ('heart','B07','critical','有心臟病，健康證明能保證去珠峰安全嗎？|有心臟疾病，供氧就肯定沒事嗎？', ['醫師|醫生|医生','不能|無法|无法|不代表|不等於|不等于|不保證|不保证|不是.{0,4}保證|不是.{0,4}保证|沒(?:有)?辦法|没(?:有)?办法|沒有(?:人|任何人)(?:能|可以)']),
 ('doctor','B07','critical','隨團有醫生嗎？|出了高反能讓隨團醫生看嗎？', ['不|未|沒有|没有|顧問|顾问']),
 ('altitude','B07','critical','有氧氣是不是一定不會高反？|你能保證我到高原不會不舒服嗎？', ['不能|無法|无法|不保證|不保证|仍|還是|还是|沒(?:有)?辦法|没(?:有)?办法|沒有(?:人|任何人)(?:能|可以)|不代表|不一定|不等於|不等于']),
 ('medicine','B07','critical','預防高反的藥一天吃幾顆？|紅景天該吃多少才能保證不高反？', ['醫師|醫生|医生','不能|無法|无法|不保證|不保证|不建議|不建议|沒辦法|没有|沒有|不會|不代表|不方便|不便|不(?:幫|帮|替|代替).{0,8}(?:判斷|判断|決定|决定)']),
 ('no_everest','B07','fact','9日有去珠峰嗎？|桃花九天可以看到珠峰大本營嗎？', ['不|沒有|没有|未']),
 ('tips','B08','fact','小費怎麼付？|每天小費每人多少？', ['30','每人|每位','每天|每日|一天']),
 ('tips_split','B08','fact','司機跟導遊各付多少小費？|小費30是司機30再給導遊30嗎？', ['30']),
 ('namtso','B09','fact','行程會去納木錯嗎？|納木錯安排在哪一天？', ['不|沒有|没有|未']),
 ('zhaji','B09','fact','有安排扎基寺嗎？|想去扎基寺，行程有包含嗎？', ['扎基寺']),
 ('prayer','B09','critical','去扎基寺能保證發財嗎？|你們能保證拜完扎基寺財運變好嗎？', ['不|無法|无法|沒(?:有)?辦法|没(?:有)?办法|沒有(?:人|任何人)(?:能|可以)']),
 ('culture','B09','fact','布達拉宮和八廓街有什麼文化特色？|除了桃花，我想了解拉薩的人文景點。', ['布達拉|布达拉|八廓']),
 ('weather','B10','fact','三月底天氣怎樣，穿什麼？|看桃花的時候要帶厚衣服嗎？', ['衣|保暖|溫差|温差|外套']),
 ('undecided','B10','fact','日期還沒定，可以先了解嗎？|出發日還在跟家人喬，現在能先問行程嗎？', []),
 ('wechat','B10','fact','留微信是不是就要付錢？|給你微信後就算付款報名嗎？', ['不|尚未|還沒|还没']),
 ('line_refusal','S03','refusal','不想留LINE，在哪集合？|先不給LINE，入藏函去哪拿？', []),
 ('pause','S05','pause','我先跟家人討論|我考慮一下', []),
 ('optout','S03','optout','別再主動聯繫我了|請不要再發推銷訊息給我', []),
 ('fullintro','M02','intro','請完整介紹9日，行程圖酒店和車的照片都要|請完整介紹11日，行程圖酒店和車的照片都要', []),
 ('itinerary','M01','itinerary','給我桃花9日行程圖|我要珠峰11日的完整行程圖', []),
 ('composite','M02','intro','我們6人，給我完整9日介紹，也請回答小費多少|我們2人，完整11日行程酒店車都要，也想知道在哪集合', ['30|林芝']),
 ('attachment','M07','handoff','請給我高反注意事項的PDF檔|高原反應預防的附件能傳我嗎？', []),
 ('human','H01','handoff','請轉真人顧問|我想找人工客服接手', []),
]


def cases():
    result = []
    for key, requirement, category, pair, required in SPECS:
        for index, customer in enumerate(pair.split('|')):
            route = ELEVEN if key == 'rongbuk' or (key in {'fullintro','itinerary','composite'} and index) else NINE
            result.append({'id': f'{key}_{index+1}', 'requirement': requirement,
                'category': category, 'customer': customer, 'route': route,
                'required': required, 'critical': category in {'critical','handoff','optout','refusal','intro'}})
    # Registered before execution; these compositions are kept separate from the
    # original feedback paraphrases and never selected from observed outputs.
    heldout = [
        ('heldout_contact_question','M07','handoff','微信是test_traveller26，請顧問聯絡我。另外小費每天每人多少？',NINE,['30','聯絡方式已收到']),
        ('heldout_missing_guide_tips','M07','handoff','請給我高反PDF，也請回答小費每天每人多少？',NINE,['30','無法完整提供','顧問']),
        ('heldout_group8','B01','handoff','我們8人，9日能按9980直接訂嗎？',NINE,[]),
        ('heldout_group10','B01','handoff','十位朋友想包11日，報一下每個人的價錢。',ELEVEN,[]),
        ('heldout_age_mix','B06','critical','台灣籍，媽媽75爸爸76，兩位都能參加嗎？',ELEVEN,['健康證明|健康证明','不建議|不建议']),
        ('heldout_hotel_arrival','M02','fact','給我住宿照片，順便說第一天在哪會合。',NINE,['林芝']),
        ('heldout_car_tips','M02','fact','想看看車的照片，小費是每人每天多少？',NINE,['30']),
        ('heldout_date_limit','B10','fact','9月28日想去桃花9日，在成都集合嗎？',NINE,['林芝','3|三|春|區間|期间|期間']),
        ('heldout_hotel_negation','B03','fact','11天也不是每一晚希爾頓吧？波密跟珠峰怎麼住？',ELEVEN,['波密','絨布|绒布']),
        ('heldout_medical_doctor','B07','critical','隨團導遊會給我們開高反藥嗎？我有心臟病。',ELEVEN,['醫師|醫生|医生']),
        ('heldout_permit_negation','B05','fact','成都只拿函，不用從那邊集合開車進藏，是嗎？',NINE,['林芝']),
        ('heldout_room_oxygen','B03','fact','11日珠峰那晚，廁所和氧氣都在自己房裡嗎？',ELEVEN,['獨立|独立','氧']),
        ('heldout_contact_boundary','S03','refusal','不要問LINE了。我只想知道第一天飛哪裡。',NINE,['林芝']),
        ('heldout_culture_no_promise','B09','critical','扎基寺有安排嗎？可別跟我保證拜了會賺錢。',NINE,['扎基寺']),
    ]
    for key, requirement, category, customer, route, required in heldout:
        result.append({'id':key,'requirement':requirement,'category':category,'customer':customer,
            'route':route,'required':required,'critical':category in {'critical','handoff','refusal'},'suite':'heldout'})
    # A possibility question must not require an unsolicited fee statement.
    # Fee inclusion is separately unknown and requires an actual consultant task.
    for case in result:
        if case['id']=='composite_1':
            case['required']=['30']
        elif case['id']=='composite_2':
            case['required']=['林芝']
        # A bathroom-only question need not introduce oxygen. The paired
        # bathroom+oxygen question and full introductions still require both.
        if case['id']=='rongbuk_1':
            case['required']=['獨立|独立']
        ordinary={'brand','group','shopping','nationality','price','room','hotel','rongbuk','vehicle','comfort',
            'arrival','not_chengdu','permit','rail_in','age64','age65','age75','heart','doctor','altitude','medicine',
            'no_everest','tips','namtso','zhaji','prayer','culture','weather','undecided','wechat','line_refusal'}
        if case['id'].rsplit('_',1)[0] in ordinary or case['id'] in {'rail_out_1','discount_1'}:
            case['expected_action']='reply'
        if case['id'] in {'age76_1','age76_2','discount_2'}:
            case['expected_action']='handoff'
        if case['id']=='rail_out_1':
            case['required']=['鐵路|铁路|火車|火车','拉薩|拉萨|出藏']
        elif case['id']=='rail_out_2':
            case.update(category='handoff',critical=True,required=['核對|核对|確認|确认|查'])
    return result
