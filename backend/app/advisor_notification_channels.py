"""Advisor-only providers behind the existing durable notification queue."""
import ssl
import smtplib
from email.message import EmailMessage
from urllib.parse import quote
import httpx
import logging
import re
from app.security import decrypt_secret


class _RedactWecomQueries(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if 'qyapi.weixin.qq.com' in message:
            record.msg = re.sub(r'(https://qyapi\.weixin\.qq\.com/[^?\s"]+)\?[^\s"]+', r'\1?[redacted]', message)
            record.args = ()
        return True


logging.getLogger('httpx').addFilter(_RedactWecomQueries())


class ProviderRejected(RuntimeError):
    """The provider explicitly rejected a submission; retry is safe."""


def send_advisor_notification(config, payload, on_submit):
    agent = payload.get('target_agent_id')
    recipient = config.get('recipients', {}).get(str(agent)) or config.get('recipients', {}).get(agent)
    if not agent or not recipient:
        raise ValueError('notification_advisor_recipient_missing')
    encrypted = config.get('encrypted_connection_secret')
    if not encrypted:
        raise ValueError('notification_connection_secret_missing')
    secret = decrypt_secret(encrypted.encode())
    text = payload['title'] + '\n' + payload['body']
    if payload.get('conversation_url'):
        text += '\n' + payload['conversation_url']
    if config['channel'] == 'email':
        message = EmailMessage()
        message['From'] = config['sender_email']
        message['To'] = recipient
        message['Subject'] = payload['title']
        # Stable ID supports tracing; it is not a guarantee of mailbox dedup.
        message['Message-ID'] = f'<ai-notification-{payload["notification_id"]}@{config["sender_email"].split("@")[1]}>'
        message.set_content(text)
        context = ssl.create_default_context()
        if config.get('smtp_security', 'ssl') == 'ssl':
            smtp = smtplib.SMTP_SSL(config['smtp_host'], config.get('smtp_port',465), timeout=10, context=context)
        else:
            smtp = smtplib.SMTP(config['smtp_host'], config.get('smtp_port',587), timeout=10)
        with smtp:
            if config.get('smtp_security') == 'starttls':
                smtp.ehlo(); smtp.starttls(context=context); smtp.ehlo()
            smtp.login(config['smtp_user'], secret)
            on_submit()
            refused = smtp.send_message(message)
            if refused:
                raise ProviderRejected('notification_email_recipient_rejected')
        return
    if config['channel'] != 'wecom':
        raise ValueError('notification_channel_invalid')
    # App credentials grant direct member messages; never broadcast to @all.
    if recipient == '@all' or '|' in recipient:
        raise ValueError('notification_wecom_recipient_invalid')
    with httpx.Client(timeout=10) as client:
        response = client.get('https://qyapi.weixin.qq.com/cgi-bin/gettoken',
            params={'corpid':config['corp_id'], 'corpsecret':secret})
        response.raise_for_status()
        token = response.json()
        if token.get('errcode',0) != 0 or not token.get('access_token'):
            raise ProviderRejected('notification_wecom_token_rejected')
        on_submit()
        response = client.post('https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=' + quote(token['access_token'],safe=''),
            json={'touser':recipient,'msgtype':'text','agentid':config['wecom_agent_id'],
                  'text':{'content':text.encode('utf-8')[:1800].decode('utf-8',errors='ignore')},'enable_duplicate_check':1,'duplicate_check_interval':1800})
        response.raise_for_status()
        result = response.json()
        if 'errcode' not in result:
            raise ValueError('notification_wecom_receipt_unknown')
        if result['errcode'] != 0 or result.get('invaliduser') or result.get('unlicenseduser'):
            raise ProviderRejected('notification_wecom_recipient_rejected')
