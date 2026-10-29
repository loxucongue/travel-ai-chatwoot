"""Narrow, meaning-preserving replacements for a third-person service referral.

Only public outgoing text uses this helper. Handoff tasks, labels, product facts,
customer history and a named person's introduction are not rewritten.
"""
import re
from app.business_profile import PROFILE


def service_voice(text: str) -> str:
    # Remove filler only before preference verbs; retain factual comparisons.
    text = re.sub(r'(您|你)(這次|这次|這趟|这趟)?(?:比較|比较)(?=想|喜歡|喜欢|期待|在意)', r'\1\2', text)
    if not PROFILE.get('rewrite_advisor_referrals', True):
        return text
    # Keep modality: "can ask an advisor" becomes "can assist", not "done".
    text = re.sub(r'(我(?:們|们)?)(可以|會|会)?[請请讓让](?:顧問|顾问)(?:接著|接着)?',
                  lambda m: m[1] + (m[2] or '') + '接著', text)
    text = re.sub(r'[請请](?:顧問|顾问)(?=確認|确认|協助|协助|核對|核对)', '我們', text)
    text = re.sub(r'(?:顧問|顾问)(?=可以|會協助|会协助|會確認|会确认)', '我們', text)
    return text
