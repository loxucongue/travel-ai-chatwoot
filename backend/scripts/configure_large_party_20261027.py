"""Install approved group reception variants without changing ordinary sales."""
from copy import deepcopy

CONTACT = '您方便加入我們的官方LINE@（@china2go）或微信（china2go-cits）嗎？也可以提供您的LINE或微信QR code，方便顧問和您確認安排。'
BRIEF = {
    'tibet_essence_8d':['itinerary_overview','everest','company'],
    'tibet_overview_10d':['itinerary_overview','everest','company'],
    'tibet_linzhi_8d':['itinerary_overview','basum','company'],
    'tibet_winter_8d':['itinerary','qiayang','followup-guests'],
    'peach_11d_2027':['itinerary_overview','peach_highlights'],
    'peach_9d_2027':['itinerary_overview','peach_highlights'],
}
EXCLUDED = {'vehicle_reference','vehicle_oxygen','vehicle','oxygen','advisor_greeting'}


def update_package(package):
    result = deepcopy(package)
    if result['route_variant'] not in BRIEF:
        return result
    groups = result['content_groups']
    overrides = {}
    for key in ('itinerary_overview','itinerary'):
        if key in groups:
            overrides[key] = '先給您看這條行程圖，這是基本路線；您們自己一團，日期、天數和想去的景點可以再和顧問討論調整。'
    if 'company' in groups:
        overrides['company'] = '我們是在地的團隊，每一季都會實際踩線，和導遊同事一起優化行程與酒店。這些是我們客人的旅遊照片～'
    if 'no_shopping' in groups:
        overrides['no_shopping'] = '您們是自己獨立一團，安排純玩行程；無購物約定會寫在合約裡。'
    result['large_party'] = {
        'enabled':True, 'minimum':7, 'source_ref':'https://china2go.com/8425-2/；2026-10-27用户确认仅7人以上，交接后机制不变',
        'introduction_sequence':[key for key in result['content_sequence']
                                 if groups[key].get('initial_delivery') and key not in EXCLUDED],
        'brief_sequence':BRIEF[result['route_variant']], 'group_text_overrides':overrides,
        'opening_text':'您們{party_description}可以安排自己獨立一團！先給您看看這條行程～',
        'completed_text':'您們{party_description}可以安排自己獨立一團！剛剛看的是基本行程，日期、天數和想去的景點可以再和顧問討論。',
        'contact_text':CONTACT,
        'unknown_route_text':'您們可以安排自己獨立一團～請問這次規劃幾天，或有特別想去的地方嗎？',
        'unknown_preference_text':'您們有特別想去的地方，或想避開的景點嗎？',
    }
    result['package_version'] = result['package_version'].split('+large-party')[0] + '+large-party-20261027'
    return result
