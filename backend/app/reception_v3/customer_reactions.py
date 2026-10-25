"""Verified sticker meaning; no guessing from filenames, sizes or resemblance."""
import base64
import hashlib
from io import BytesIO
import re

from PIL import Image

# Visually verified original attachments from the 2026-10-20 and 2026-10-22
# business reports. A decoded-pixel digest tolerates PNG metadata/compression
# changes, but never resized, altered, animated or merely similar pictures.
VERIFIED_STICKERS = {
    '9f0a5eafc0cef32f2e39d1fd2e54faaf29f77943a85431dd6243e00a8347da39': 'thumbs_up',
}
REACTION_TEXT = '已识别为点赞贴图，意思相当于「嗯嗯／收到」。不是图片咨询，也不表示同意报名、日期、换线、加好友或独立成团。'


def recognize_sticker(encoded):
    try:
        if len(encoded) > 12 * 1024 * 1024:
            return None
        data = base64.b64decode(encoded.split(',', 1)[1], validate=True)
        with Image.open(BytesIO(data)) as image:
            if max(image.size) > 256 or getattr(image, 'n_frames', 1) != 1:
                return None
            rgba = image.convert('RGBA')
            digest = hashlib.sha256(f'{rgba.width}x{rgba.height}:RGBA:'.encode() + rgba.tobytes()).hexdigest()
        if digest in VERIFIED_STICKERS:
            return {'kind': VERIFIED_STICKERS[digest], 'pixel_sha256': digest,
                    'evidence': 'verified_sticker_pixels'}
    except (ValueError, IndexError, OSError, Image.DecompressionBombError):
        pass
    return None


def stored_reaction(message):
    value = message.get('customer_reaction') or {}
    if (message.get('direction') == 'incoming' and isinstance(value, dict)
            and value.get('evidence') == 'verified_sticker_pixels'
            and value.get('kind') == VERIFIED_STICKERS.get(value.get('pixel_sha256'))
            and value.get('kind') and value.get('attachment_ids')
            and None not in value['attachment_ids']
            and value['attachment_ids'] == [a.get('id') for a in message.get('attachments', [])]):
        return {k: value[k] for k in ('kind', 'pixel_sha256', 'evidence', 'attachment_ids')}
    return None


def pending_messages(context):
    if context.get('event') != 'customer_message':
        return []
    incoming = [m for m in context.get('messages', []) if m.get('direction') == 'incoming']
    ids = {m.get('id') for m in context.get('buffered_questions', [])}
    return [m for m in incoming if m.get('id') in ids] if ids else incoming[-1:]


def only_acknowledgements(context, reactions):
    pending = pending_messages(context)
    # This guard applies to verified images only. Any accompanying question or
    # an additional unrecognized attachment remains the normal Agent's task.
    return bool(pending) and all(m.get('id') in reactions and
        re.fullmatch(r'[\s👍🏻🏼🏽🏾🏿\ufe0f]*', m.get('content') or '') for m in pending)


def invalid_ack_decision(decision, context):
    """A sticker alone cannot authorize a handoff, selection or profile update."""
    if (decision['action'] == 'handoff' or decision.get('profile')
            or decision.get('route_choice', 'unchanged') != 'unchanged'
            or decision.get('route_variant') not in ('', context.get('route_variant', ''))
            or decision.get('start_introduction') or decision.get('interrupt')
            or decision.get('opt_out') is not None or decision.get('stop_followup')
            or decision.get('next_check_minutes') is not None):
        return True
    return any(m.get('script_id') or m.get('asset_keys') or
        re.search(r'看不清|看清|看不到|無法.*(?:圖|辨識)|无法.*(?:图|识别)|重新.*(?:圖|图)|'
                  r'用文字|QR\s*Code|二維碼|二维码|加好友|LINE|微信', m.get('text', ''), re.I)
        for m in decision.get('messages', []))


def remember_reactions(messages, logs):
    verified = {}
    for log in logs:
        if log.get('node') == 'customer_reactions':
            for message_id, value in log.get('recognized', {}).items():
                verified[message_id] = value
    result = []
    for message in messages:
        candidate = {**message, 'customer_reaction': verified.get(message.get('id'))}
        value = stored_reaction(candidate)
        result.append({**message, 'customer_reaction': value} if value else message)
    return result
