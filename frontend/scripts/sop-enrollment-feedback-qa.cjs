const { chromium } = require('C:/Users/24159/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const output = path.resolve(__dirname, '../../output/playwright/sop-enrollment-feedback');
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const errors = [];
  const writes = [];
  let fail = true;
  const sop = { id: 1, name: '本地行程跟进', status: 'running', published_version: 1, version: 1,
    updated_at: '2026-08-27T00:00:00Z', trigger_type: 'manual', trigger_labels: [], exit_labels: [],
    inbox_ids: [128859], nodes: [{ key: 'ten', schedule_type: 'relative', delay_minutes: 10,
      messages: [{ key: 'text', content_type: 'text', content: '行程参考资料' }] }], dry_run: true, live_enabled: false };
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/v1/**', async route => {
      const request = route.request();
      const endpoint = new URL(request.url()).pathname.replace('/v1', '');
      if (endpoint === '/sops/1/enroll') {
        writes.push(request.postDataJSON());
        return route.fulfill({ status: fail ? 409 : 200, json: fail
          ? { error: { code: 'sop_round_active', message: '已有进行中的轮次，不能重复入组' } }
          : { enrolled: 1, outbound: false, rounds: [{ round_number: 1, status: 'active' }], session_ids: [1] } });
      }
      const data = {
        '/auth/me': { id: 1, email: 'qa@example.invalid', display_name: 'QA', roles: ['admin'], permissions: ['*'], must_change_password: false },
        '/auth/csrf': { csrf_token: 'isolated-qa' },
        '/notifications': { items: [], unread: 0 },
        '/sops': { items: [sop] },
        '/sops/1/enrollment-candidates': { items: [{ id: 26, name: 'QA 客户', inbox: '测试收件箱', round_number: 0, round_status: null }], total: 1 },
      }[endpoint];
      assert(data, `Unexpected request: ${endpoint}`);
      await route.fulfill({ json: data });
    });
    await page.goto('http://127.0.0.1:4175/#/sops');
    await page.getByTitle('首次入组', { exact: true }).click();
    await page.getByRole('dialog').getByRole('checkbox').check();
    await page.getByRole('button', { name: '确认入组', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '已有进行中的轮次' }).waitFor();
    assert.equal(await page.locator('.toast').count(), 0);
    await page.screenshot({ path: path.join(output, 'error-desktop.png') });
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await page.getByTitle('首次入组', { exact: true }).click();
    assert.equal(await page.getByRole('dialog').getByRole('alert').count(), 0);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('dialog').getByRole('checkbox').check();
    await page.screenshot({ path: path.join(output, 'enrollment-mobile.png') });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    fail = false;
    await page.getByRole('button', { name: '确认入组', exact: true }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    await page.getByText('SOP 已更新', { exact: true }).waitFor();
    assert.equal(writes.length, 2);
    assert(writes.every(body => body.environment === 'shadow' && body.conversation_ids[0] === 26));
    assert.deepEqual(errors, []);
    console.log('SOP enrollment success/error feedback and mobile layout passed. API mocked; no customer writes.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
