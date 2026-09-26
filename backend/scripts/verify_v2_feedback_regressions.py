"""Real-model negative controls for the V2 fact/response verifier; no delivery."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
from unittest.mock import patch
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.deepseek_evaluation import EvaluationDecision
from app.reception_v2.runtime import _verify

CASES = [
    ('wrong_minimum_age', '台灣旅客64歲可以嗎？', '報名年齡是65至75歲，64歲不在範圍，需要特批。', ['service.peach_age'], False),
    ('nationality_exemption', '香港人70歲要健康證明嗎？', '香港旅客不在健康證明要求內，不用提交。', ['service.peach_age'], False),
    ('wrong_route_difference', '9日和11日有什麼差別？', '11日比9日多出扎什倫布寺及拉日鐵路。', ['route.9.days', 'route.11.days'], False),
    ('off_topic_expansion', '在哪集合？', '林芝集合，第一天接機。D2南迦巴瓦峰、魯朗林海，D3波密桃花溝，D8珠峰大本營。', ['service.peach_arrival', 'route.11.days'], False),
    ('correct_age', '台灣旅客75歲可以嗎？', '75歲可以報名，台灣長者需要提交健康證明，個人健康狀況仍要由醫師評估。', ['service.peach_age'], True),
    ('correct_meeting', '在哪集合？', '集合地點是林芝，第一天安排林芝接機；成都則是由專人交付入藏函。', ['service.peach_arrival'], True),
]


def main():
    output = Path(sys.argv[1])
    original = httpx.Client.send
    def guarded(client, request, *args, **kwargs):
        if str(request.url) != settings.deepseek_base_url.rstrip('/') + '/chat/completions' or request.method != 'POST':
            raise RuntimeError('evaluation_network_destination_blocked')
        return original(client, request, *args, **kwargs)
    rows = []
    with patch.object(httpx.Client, 'send', guarded):
        for key, question, reply, refs, expected in CASES:
            row = {'key': key, 'expected_acceptance': expected}
            try:
                decision = EvaluationDecision(action='reply', branch='peach_11d', intent='other',
                    route_variant='peach_11d_2027', reply=reply, evidence_refs=refs)
                result, _, _ = _verify({'engine_version': 'v2', 'module': 'reply', 'customer_text': question}, decision)
                accepted = bool(result.supported and result.relevant and not result.contract_violations)
                row.update(result=asdict(result), accepted=accepted, passed=accepted == expected)
            except Exception as exc:
                row.update(error=str(exc), passed=False)
            rows.append(row)
            output.write_text(json.dumps({'cases': rows, 'outbound': False}, ensure_ascii=False, indent=2), encoding='utf-8')
            print(key, row['passed'], flush=True)


if __name__ == '__main__':
    main()
