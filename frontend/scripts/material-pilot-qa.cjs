const { chromium } = require('C:/Users/24159/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const root = path.resolve(__dirname, '../..');
const output = path.join(root, 'output/playwright/material-pilot');
fs.mkdirSync(output, { recursive: true });

(async () => {
  const credentials = JSON.parse(fs.readFileSync(path.join(root, '.runlogs/material-qa.json'), 'utf8'));
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://127.0.0.1:8000/**', route => route.continue({ url: route.request().url().replace(':8000/', ':8002/') }));
    await page.goto('http://127.0.0.1:4175/');
    await page.locator('input[type=email]').fill(credentials.email);
    await page.locator('input[type=password]').fill(credentials.password);
    const loginResponse = page.waitForResponse(r => r.url().endsWith('/v1/auth/login') && r.request().method() === 'POST');
    await page.getByRole('button', { name: /登录/ }).click();
    const login = await loginResponse;
    assert(login.ok(), `login status ${login.status()}`);
    const csrf = (await login.json()).csrf_token;
    await page.waitForURL('**/#/overview');
    const get = async url => {
      const response = await page.request.get('http://127.0.0.1:8002/v1' + url);
      assert(response.ok(), `${url}: ${response.status()}`);
      return response.json();
    };
    const post = async (url, body) => {
      const response = await page.request.post('http://127.0.0.1:8002/v1' + url, { headers: { 'X-CSRF-Token': csrf, Origin: 'http://127.0.0.1:4175' }, data: body ?? {} });
      assert(response.ok(), `${url}: ${response.status()} ${await response.text()}`);
      return response.json();
    };
    const strategies = (await get('/sops')).items;
    const sop = strategies.find(x => x.route_variant === 'peach_9d_2027');
    assert(sop && sop.test_conversation_ids.join(',') === '26');
    const session = await post('/playground/sessions', { mode: 'sop', inbox_binding_id: 1, virtual_now: '2026-08-26T10:00:00+08:00' });
    await page.goto(`http://127.0.0.1:4175/#/playground?session=${session.id}`);
    await page.getByLabel('模拟客户消息', { exact: true }).fill('想看2027年桃花9日的行程圖，不上珠峰。');
    await page.getByRole('button', { name: '发送模拟', exact: true }).click();
    await page.getByRole('button', { name: '模拟整组送达', exact: true }).waitFor({ timeout: 45000 });
    await page.waitForFunction(() => [...document.querySelectorAll('.playground-messages img')].some(x => x.complete && x.naturalWidth > 0));
    await page.screenshot({ path: path.join(output, 'ai-image-draft-desktop.png') });
    await page.getByRole('button', { name: '模拟整组送达', exact: true }).click();
    await page.getByLabel('选择 SOP', { exact: true }).selectOption(String(sop.id));
    await page.getByLabel('选择发布版本').selectOption({ index: 1 });
    await page.getByRole('button', { name: '首次入组', exact: true }).click();
    await page.getByText('第 1 轮', { exact: true }).waitFor();
    for (let i = 0; i < 3; i++) {
      await page.getByLabel('推进分钟数', { exact: true }).fill('10');
      const response = page.waitForResponse(r => r.url().includes(`/sessions/${session.id}/advance`) && r.request().method() === 'POST');
      await page.getByTitle('推进模拟时间', { exact: true }).click();
      assert((await response).ok());
    }
    const current = await get(`/playground/sessions/${session.id}`);
    assert.deepStrictEqual(current.jobs.map(x => x.status), ['already_provided', 'simulated_delivered', 'simulated_delivered']);
    assert.equal(current.messages.filter(x => x.media_id && x.status === 'simulated_delivered').length, 3);
    await page.getByText('素材此前已提供，本组跳过', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, 'sop-dedup-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(350);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.locator('.playground-messages').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'ai-image-mobile.png') });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto('http://127.0.0.1:4175/#/sops');
    const row = page.locator('.sop-row').filter({ hasText: sop.name });
    await row.getByTitle('编辑草稿', { exact: true }).click();
    assert.equal(await page.getByLabel('适用线路', { exact: true }).inputValue(), 'peach_9d_2027');
    await page.getByRole('button', { name: '素材库', exact: true }).first().click();
    await page.waitForFunction(() => { const imgs = [...document.querySelectorAll('.material-picker-grid img')]; return imgs.length > 0 && imgs.every(x => x.complete && x.naturalWidth > 0); });
    await page.locator('.sop-preview-timeline li').first().waitFor();
    await page.locator('.material-picker').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'material-picker-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('.material-picker').scrollIntoViewIfNeeded();
    await page.waitForTimeout(350);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: path.join(output, 'material-picker-mobile.png') });
    await page.getByRole('button', { name: '关闭 SOP 编辑器', exact: true }).click();
    assert.deepStrictEqual(errors, []);
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ passed: true, aiDraft: true, orderedSopGroups: true, crossModuleDedupe: true, materialPreview: true, mobile: true, pageErrors: errors, ai_ms: current.runs[0]?.trace.total_ms, realCustomerSends: 0 }, null, 2));
    console.log('Material pilot browser checks passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
