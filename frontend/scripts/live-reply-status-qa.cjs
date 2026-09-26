const { chromium } = require('C:/Users/24159/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const output = path.resolve(__dirname, '../../output/playwright/live-reply-status');
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const errors = [];
  let state = 'failed';
  const conversation = { id: 26, name: '本地测试会话', channel: 'Channel::FacebookPage', inbox: '测试收件箱', inbox_id: 128859,
    last_message: '9月啊', ai_state: 'AI_ACTIVE', ai_reason: 'inbox_enabled', labels: ['ai'], can_reply: true,
    status: 'open', ai_mode: 'enabled', ai_mode_source: 'chatwoot', ai_label_present: true, ai_sync_status: 'synced',
    version: 1, updated_at: '2026-08-26T16:00:00Z', chatwoot_url: 'https://example.invalid' };
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
    page.on('pageerror', error => errors.push(error.message));
    // All API traffic is isolated; no login credentials or real customer writes.
    await page.route('**/v1/**', async route => {
      const endpoint = new URL(route.request().url()).pathname.replace('/v1', '');
      const data = {
        '/auth/me': { id: 1, email: 'qa@example.invalid', display_name: 'QA', roles: ['admin'], permissions: ['*'], must_change_password: false },
        '/auth/csrf': { csrf_token: 'isolated-qa' },
        '/notifications': { items: [], unread: 0 },
        '/conversations/filters': { inboxes: [], labels: [{ title: 'ai', color: '#155e75' }], ai_states: ['AI_ACTIVE'] },
        '/conversations': { items: [conversation], total: 1, page: 1, page_size: 25 },
        '/conversations/26': { ...conversation, contact: { name: conversation.name, pii_masked: false },
          latest_ai_reply: { status: state, failure_kind: 'model_timeout', created_at: '2026-08-26T16:00:00Z', completed_at: '2026-08-26T16:01:00Z' } },
        '/conversations/26/controls': { assignee: null, agents: [], labels: [], conversation_labels: ['ai'], can_create_labels: false },
        '/conversations/26/messages': { items: [
          { id: 1, direction: 'incoming', content: '想看桃花9日行程', attribution: 'customer', created_at: '2026-08-26T15:59:00Z' },
          { id: 2, direction: 'outgoing', content: '行程资料供您参考。', attribution: 'ai', status: 'sent', created_at: '2026-08-26T15:59:10Z' },
          { id: 3, direction: 'outgoing', content: '另一条测试消息，尚未取得渠道回执。', attribution: 'ai', status: 'submitted', created_at: '2026-08-26T15:59:15Z' },
          { id: 4, direction: 'incoming', content: '9月啊', attribution: 'customer', created_at: '2026-08-26T16:00:00Z' },
        ] },
      }[endpoint];
      assert(data, `Unexpected endpoint: ${endpoint}`);
      await route.fulfill({ json: data });
    });
    await page.goto('http://127.0.0.1:4175/#/conversations');
    await page.getByText('本轮 AI 回复超时，未自动重发，请人工跟进。', { exact: true }).waitFor();
    await page.getByText('已发送', { exact: true }).waitFor();
    await page.getByText('已提交', { exact: true }).waitFor();
    assert.equal(await page.getByText('发送中', { exact: true }).count(), 0);
    await page.screenshot({ path: path.join(output, 'desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('.reply-issue').scrollIntoViewIfNeeded();
    await page.waitForTimeout(200);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    const bounds = await page.locator('.reply-issue').evaluate(element => ({ width: element.clientWidth, scroll: element.scrollWidth }));
    assert(bounds.scroll <= bounds.width + 1);
    await page.screenshot({ path: path.join(output, 'mobile.png') });
    state = 'retrying';
    await page.reload();
    await page.getByText('AI 服务暂时未响应，本轮正在重试。', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, 'retrying-mobile.png') });
    state = 'submitted';
    await page.reload();
    await page.getByText('已发送', { exact: true }).waitFor();
    assert.equal(await page.locator('.reply-issue').count(), 0);
    assert.deepEqual(errors, []);
    console.log('Desktop/mobile receipt labels, timeout notice and later success: passed. All API requests mocked.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
