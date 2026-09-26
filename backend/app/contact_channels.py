"""Canonical channels and backward-compatible customer-facing refusal scopes."""
CHANNEL_LABELS = {'line': 'LINE', 'wechat': '微信', 'phone': '电话',
                  'email': 'Email', 'whatsapp': 'WhatsApp'}


def refusal_scope(value):
    if value == 'all':
        return 'all'
    aliases = {**{k: v for k, v in CHANNEL_LABELS.items()},
               **{v.casefold(): v for v in CHANNEL_LABELS.values()}, '電話': '电话'}
    return aliases.get(str(value or '').strip().casefold())
