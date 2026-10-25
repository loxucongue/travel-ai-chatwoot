"""Render the exported JSON without changing any original message content."""
import html
import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

from export_chatwoot_transcripts import render_markdown

p = Path(sys.argv[1])
r = json.loads((p / 'transcripts.json').read_text(encoding='utf-8'))
r['pairing_rule'] = '连续客户消息与随后至下一次客户发言前的客服回复按时间分组，不保证语义逐句对应。'
r['attribution_limit'] = '坐席账号不能证明由人工操作；缺少发送者信息的出站消息归属未知。'
labels = {'agent_account': '坐席账号（人工/自动化未核实）', 'unknown': '客服侧（发送者未知）', 'ai_or_bot': '机器人账号'}

def show(m):
    t = m['created_at']
    if isinstance(t, (int, float)):
        t = datetime.fromtimestamp(t, timezone(timedelta(hours=8))).isoformat()
    role = '客户' if m['role'] == 'customer' else labels[m['advisor_kind']]
    attachment = '<small>含附件；附件内容未转写。</small>' if m['has_attachments'] else ''
    return '<article><small>' + html.escape(f"{role} | {t} | 消息 #{m['message_id']}") + '</small><pre>' + html.escape(m['content']) + '</pre>' + attachment + '</article>'

parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>客户原话与对应回复</title><style>body{font:16px system-ui;margin:32px auto;max-width:1300px;padding:0 20px;color:#182b40;background:#f4f6f8}table{width:100%;table-layout:fixed;border-collapse:collapse}td,th{width:50%;vertical-align:top;border:1px solid #cad3dc;padding:14px;background:white}th{background:#e5edf5}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:10px 0}small{color:#536779}article+article{border-top:1px dashed #ccd6df;margin-top:14px;padding-top:14px}summary{cursor:pointer;font-weight:bold;padding:18px;background:#e5edf5}details{margin:16px 0}</style><h1>客户原话与对应回复</h1>',
    f"<p>{r['conversation_count']} 个会话 · 客户消息 {r['customer_message_count']} 条 · 客服侧回复 {r['consultant_message_count']} 条。时间显示为 UTC+8。</p>",
    '<p>' + r['pairing_rule'] + '</p><p>' + r['attribution_limit'] + '</p>',
    '<p>保留原文，不改写、不去重。附件只标记存在，未识别图片或转写语音。私有备注、系统事件、已删除消息不在此文件中。所有会话均保留，未排除测试会话。</p>']
for c in r['conversations']:
    parts.append(f'<details><summary>会话 #{c["conversation_id"]} · 客户 {c["customer_message_count"]} 条 / 回复 {c["consultant_message_count"]} 条</summary><table><tr><th>客户原话</th><th>随后客服侧回复</th></tr>')
    for t in c['turns']:
        a = ''.join(show(m) for m in t['customer_messages']) or '（此前无客户发言：客服主动消息）'
        b = ''.join(show(m) for m in t['consultant_replies']) or '（截至抓取时未见后续公开回复）'
        parts.append('<tr><td>' + a + '</td><td>' + b + '</td></tr>')
    parts.append('</table></details>')
parts.append('</html>')
(p / 'customer-reply-pairs.html').write_text(''.join(parts), encoding='utf-8')
(p / 'transcripts.json').write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding='utf-8')
(p / 'transcripts.md').write_text(render_markdown(r), encoding='utf-8')
assert '客户原话与对应回复' in (p / 'customer-reply-pairs.html').read_text(encoding='utf-8')
assert sum(len(t['customer_messages']) + len(t['consultant_replies']) for c in r['conversations'] for t in c['turns']) == r['customer_message_count'] + r['consultant_message_count']
print('UTF-8 and message coverage verified')
