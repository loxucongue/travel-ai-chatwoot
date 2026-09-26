const { chromium } = require('C:/Users/24159/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const root = path.resolve(__dirname, '../..');
const output = path.join(root, 'output/playwright/sop-rounds');
fs.mkdirSync(output, { recursive: true });

(async () => {
  const credentials = JSON.parse(fs.readFileSync(path.join(root, '.runlogs/three-qa-credentials.json'), 'utf8'));
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:4175/');
    await page.locator('input[type=email]').fill(credentials.email);
    await page.locator('input[type=password]').fill(credentials.password);
    const loginResponse = page.waitForResponse(r => r.url().endsWith('/v1/auth/login') && r.request().method() === 'POST');
    await page.getByRole('button', { name: /登录/ }).click();
    await page.waitForURL('**/#/overview');
    // The isolated test database contains no real account credentials or customer data.
    const login = await loginResponse;
    const csrf = (await login.json()).csrf_token;
    const post = async (url, body) => {
      const response = await page.request.post('http://127.0.0.1:8000/v1' + url, { headers: { 'X-CSRF-Token': csrf, Origin: 'http://127.0.0.1:4175' }, data: body ?? {} });
      assert(response.ok(), `${url}: ${response.status()} ${await response.text()}`);
      return response.json();
    };
    const sop = await post('/sops', { name: 'QA-rounds-' + Date.now(), trigger_type: 'label', trigger_labels: ['qa-start'], exit_labels: ['qa-stop'], nodes: [
      { key: 'ten', schedule_type: 'relative', basis: 'enrollment', delay_minutes: 10, messages: [{ key: 'text', content_type: 'text', content: 'QA first group' }] },
      { key: 'twenty', schedule_type: 'relative', basis: 'enrollment', delay_minutes: 20, messages: [{ key: 'text', content_type: 'text', content: 'QA second group' }] },
    ] });
    await post(`/sops/${sop.id}/publish`);
    const session = await post('/playground/sessions', { mode: 'sop', inbox_binding_id: 1, virtual_now: '2026-08-26T10:00:00+08:00' });
    // Seed a customer observation without enqueuing the paid model.
    await page.goto(`http://127.0.0.1:4175/#/playground?session=${session.id}`);
    await page.getByLabel('选择 SOP', { exact: true }).selectOption(String(sop.id));
    await page.getByLabel('选择发布版本').selectOption({ index: 1 });
    await page.getByRole('button', { name: '首次入组', exact: true }).click();
    await page.getByText('第 1 轮', { exact: true }).waitFor();
    assert(await page.getByRole('button', { name: '重新入组', exact: true }).isDisabled());
    await page.getByRole('switch', { name: '人工接管', exact: true }).click();
    await page.getByText('已取消', { exact: true }).first().waitFor();
    await page.getByRole('switch', { name: '人工接管', exact: true }).click();
    await page.getByRole('button', { name: '重新入组', exact: true }).click();
    await page.getByText('第 2 轮', { exact: true }).waitFor();
    const current = await (await page.request.get(`http://127.0.0.1:8000/v1/playground/sessions/${session.id}`)).json();
    assert.equal(current.enrollments.length, 2);
    await page.screenshot({ path: path.join(output, 'round-two-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right <= 0);
    await page.getByText('第 2 轮', { exact: true }).scrollIntoViewIfNeeded();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: path.join(output, 'round-two-mobile.png') });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto('http://127.0.0.1:4175/#/sops');
    const row = page.locator('.sop-row').filter({ hasText: sop.name });
    await row.getByTitle('首次入组', { exact: true }).click();
    await page.getByRole('dialog').getByText('QA Customer', { exact: true }).click();
    await page.getByRole('button', { name: '确认入组', exact: true }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    await row.getByTitle('重新入组', { exact: true }).click();
    await page.getByRole('dialog').getByText(/第 1 轮 进行中/).waitFor();
    assert(await page.getByRole('dialog').getByRole('checkbox').isDisabled());
    await page.screenshot({ path: path.join(output, 'active-round-blocked.png') });
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await row.getByTitle('编辑草稿', { exact: true }).click();
    assert.equal(await page.getByLabel('入组事件').inputValue(), 'label');
    await page.getByRole('checkbox', { name: 'qa-start', exact: true }).first().waitFor();
    await page.screenshot({ path: path.join(output, 'tag-configuration.png') });
    assert.deepStrictEqual(errors, []);
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ passed: true, explicit_round_two: true, active_round_disabled: true, tag_controls: true, mobile: true, customer_sends: 0, errors }, null, 2));
    console.log('SOP rounds browser checks passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
