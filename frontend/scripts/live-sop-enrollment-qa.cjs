const { chromium } = require('C:/Users/24159/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const output = path.resolve(__dirname, '../../output/playwright/live-sop-enrollment');
fs.mkdirSync(output, { recursive: true });

const sop = {
  id: 5,
  name: '2027 林芝桃花 9 日真实测试',
  description: '测试账号专用发布版本',
  status: 'running',
  published_version: 2,
  version: 5,
  updated_at: '2026-08-27T01:00:00Z',
  trigger_type: 'manual',
  trigger_labels: [],
  exit_labels: ['人工接管', '客诉'],
  inbox_ids: [128859],
  nodes: [{
    key: 'after_enrollment_10m',
    basis: 'enrollment',
    schedule_type: 'relative',
    delay_minutes: 10,
    messages: [
      { key: 'intro', content_type: 'text', content: '9 日行程参考资料' },
      { key: 'route_image', content_type: 'image', media_id: 39 },
    ],
  }],
  dry_run: true,
  live_enabled: false,
  test_conversation_ids: [26],
  rehearsal_enrolled: 0,
  rehearsal_sent: 0,
  rehearsal_blocked: 0,
  has_unpublished_changes: true,
  published_nodes: [
    { key: 'route', basis: 'enrollment', schedule_type: 'relative', delay_minutes: 10, messages: [] },
    { key: 'hotel', basis: 'enrollment', schedule_type: 'relative', delay_minutes: 20, messages: [] },
    { key: 'vehicle', basis: 'previous_node', schedule_type: 'relative', delay_minutes: 10, messages: [] },
  ],
};

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const errors = [];
  const writes = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/v1/**', async route => {
      const request = route.request();
      const endpoint = new URL(request.url()).pathname.replace('/v1', '');
      if (endpoint === '/sops/5/enroll') {
        writes.push(request.postDataJSON());
        return route.fulfill({
          json: {
            enrolled: 1,
            outbound: true,
            environment: 'live_test',
            rounds: [{ round_number: 1, status: 'active' }],
          },
        });
      }
      const data = {
        '/auth/me': { id: 1, email: 'qa@example.invalid', display_name: 'QA', roles: ['admin'], permissions: ['*'], must_change_password: false },
        '/auth/csrf': { csrf_token: 'isolated-qa' },
        '/notifications': { items: [], unread: 0 },
        '/sops': { items: [sop] },
        '/sops/options': { delivery_mode: 'allowlisted_live_test', live_test_enabled: true, live_test_conversation_ids: [26] },
        '/sops/5/enrollment-candidates': { items: [{ id: 26, name: 'Xc Luo', inbox: 'CITS 国旅环球 - China2Go', round_number: 0, round_status: null }], total: 1 },
      }[endpoint];
      assert(data, `Unexpected request: ${endpoint}`);
      await route.fulfill({ json: data });
    });

    await page.goto('http://127.0.0.1:4175/#/sops');
    await page.getByText('真实测试已启用 · 仅会话 #26', { exact: true }).waitFor();
    await page.getByTitle('首次入组', { exact: true }).click();
    await page.getByLabel('入组模式').selectOption('live_test');
    const dialog = page.getByRole('dialog');
    await dialog.getByText('Xc Luo', { exact: true }).waitFor();
    await dialog.getByText('当前草稿 v5 尚未发布', { exact: true }).waitFor();
    await dialog.getByText(/本次入组仍执行已发布 v2：入组后 10 分钟 · 入组后 20 分钟 · 上一组发送后 10 分钟/).waitFor();
    assert.equal(await dialog.getByText(/#26/).count(), 1);
    assert.equal(await dialog.locator('.selection-list input[type="checkbox"]').count(), 1);

    await dialog.locator('.selection-list input[type="checkbox"]').check();
    const submit = dialog.getByRole('button', { name: '确认真实入组', exact: true });
    assert(await submit.isDisabled(), 'Real enrollment must remain disabled before explicit confirmation.');
    await page.screenshot({ path: path.join(output, 'confirmation-required-desktop.png'), fullPage: true });

    await dialog.getByText('我确认这会向真实 Facebook 测试账号发送', { exact: true }).click();
    await dialog.getByText('允许本轮重复发送已提供素材', { exact: true }).click();
    assert(!(await submit.isDisabled()), 'Real enrollment should be enabled after target selection and confirmation.');

    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'confirmed-mobile.png'), fullPage: true });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));

    await submit.click();
    await dialog.waitFor({ state: 'hidden' });
    await page.getByText('真实 SOP 测试已入组，将按发布时间执行', { exact: true }).waitFor();
    assert.equal(writes.length, 1);
    assert.equal(writes[0].environment, 'live_test');
    assert.equal(writes[0].confirm_live_delivery, true);
    assert.equal(writes[0].allow_repeat_delivery, true);
    assert.deepEqual(writes[0].conversation_ids, [26]);
    assert.equal(typeof writes[0].request_key, 'string');
    assert(writes[0].request_key.length > 10);
    assert.deepEqual(errors, []);
    console.log('Live SOP enrollment UI passed. API mocked; no Chatwoot request was made.');
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
