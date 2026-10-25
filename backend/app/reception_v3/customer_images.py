"""Forward only bounded, trusted Chatwoot image attachments to native vision."""
from urllib.parse import urlsplit
import base64
from io import BytesIO
import time
import httpx
from PIL import Image
from app.config import settings

MAX_IMAGES = 4
MAX_BYTES = 8 * 1024 * 1024
FIELDS = ('id', 'file_type', 'file_name', 'filename', 'content_type', 'file_size')


def image_url(attachment):
    if attachment.get('file_type') != 'image':
        return None
    size = attachment.get('file_size')
    if size is not None and (type(size) is not int or size < 0 or size > MAX_BYTES):
        return None
    value = attachment.get('data_url')
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        parsed = urlsplit(value)
        allowed = {h.strip().casefold() for h in settings.customer_image_allowed_hosts.split(',') if h.strip()}
        if (parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.password
                or parsed.port not in (None, 443) or not parsed.path.startswith('/rails/active_storage/')
                or any(c in value for c in ('\r', '\n', '\\'))):
            return None
    except ValueError:
        return None
    return value


def attachment_metadata(attachments):
    result = []
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        item = {k: a[k] for k in FIELDS if k in a}
        url = image_url(a)
        if url:
            item['data_url'] = url
        result.append(item)
    return result


def select_images(context):
    """Only the current customer turn, never historical or outbound product images."""
    if context.get('event') != 'customer_message':
        return {}
    incoming = [m for m in context.get('messages', []) if m.get('direction') == 'incoming']
    pending = {m.get('id') for m in context.get('buffered_questions', [])}
    if not pending and incoming:
        pending = {incoming[-1].get('id')}
    result, seen = {}, set()
    for m in reversed(incoming):
        if m.get('id') not in pending:
            continue
        parts = []
        for a in m.get('attachments') or []:
            url = image_url(a)
            if url and url not in seen and len(seen) < MAX_IMAGES:
                seen.add(url)
                parts.append({'type': 'image_url', 'image_url': {'url': url, 'detail': 'auto'}})
        if parts:
            result[m.get('id')] = parts
    return result


def download_image(url, deadline):
    """Fetch only known Chatwoot/storage hosts, with bounded redirects and bytes."""
    storage = {h.strip().casefold() for h in settings.customer_image_storage_hosts.split(',') if h.strip()}
    with httpx.Client(follow_redirects=False) as client:
        for _ in range(4):
            parsed = urlsplit(url)
            trusted = image_url({'file_type':'image','data_url':url})
            if (not trusted and (parsed.scheme!='https' or parsed.hostname not in storage
                    or parsed.username or parsed.password or parsed.port not in (None,443))) or any(c in url for c in ('\r','\n','\\')):
                raise ValueError('image_redirect_not_allowed')
            remaining = deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('image_read_timeout')
            with client.stream('GET',url,timeout=min(remaining,8)) as response:
                if response.is_redirect:
                    location=response.headers.get('location')
                    if not location:raise ValueError('image_redirect_missing')
                    url=str(response.url.join(location));continue
                response.raise_for_status()
                if int(response.headers.get('content-length','0'))>MAX_BYTES:raise ValueError('image_too_large')
                data=bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic()>deadline:raise TimeoutError('image_read_timeout')
                    data.extend(chunk)
                    if len(data)>MAX_BYTES:raise ValueError('image_too_large')
            with Image.open(BytesIO(data)) as img:
                if img.format not in ('JPEG','PNG','GIF','WEBP') or max(img.size)>8192:
                    raise ValueError('unsupported_image')
                mime=Image.MIME[img.format]
                img.verify()
            return 'data:'+mime+';base64,'+base64.b64encode(data).decode('ascii')
    raise ValueError('too_many_image_redirects')


def readable_images(context):
    if not settings.customer_image_vision_enabled:
        return {}
    return read_customer_media(context)[0]


def read_customer_media(context):
    from app.reception_v3.customer_reactions import recognize_sticker, stored_reaction, pending_messages
    enabled = settings.customer_sticker_recognition_enabled
    reactions = {m['id']: stored_reaction(m) for m in context.get('messages', [])
                 if enabled and m.get('id') and stored_reaction(m)}
    if not settings.customer_image_vision_enabled and not enabled:
        return {}, {}
    selected=select_images(context);result={};deadline=time.monotonic()+12
    pending = {m.get('id'): m for m in pending_messages(context)}
    for message_id,parts in selected.items():
        if message_id in reactions:
            continue
        recognized = []
        for part in parts:
            try:encoded=download_image(part['image_url']['url'],deadline)
            except (httpx.HTTPError,ValueError,OSError,TimeoutError,Image.DecompressionBombError):continue
            reaction = recognize_sticker(encoded) if enabled else None
            if reaction:
                recognized.append(reaction)
            if settings.customer_image_vision_enabled:
                result.setdefault(message_id,[]).append({'type':'image_url','image_url':{'url':encoded,'detail':'auto'}})
        # Do not hide an unknown second image, a QR code, a file or an audio
        # attachment just because one of the message's images was a thumbs-up.
        if recognized and len(recognized) == len(pending.get(message_id, {}).get('attachments', [])):
            reactions[message_id] = {**recognized[0], 'attachment_ids':
                                    [a.get('id') for a in pending[message_id]['attachments']]}
            result.pop(message_id, None)
    return result, reactions
