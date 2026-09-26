const { chromium } = require('C:/Users/24159/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const root = path.resolve(__dirname, '../..');
const output = path.join(root, 'output/playwright/sop-groups');
fs.mkdirSync(output, { recursive: true });
// CC0 browser test clip, never used as a tourism business asset.
const videoPath = path.join(output, 'qa-sample.webm');
if (!fs.existsSync(videoPath)) require('child_process').execFileSync('curl.exe', ['-L', '--fail', '--max-time', '40', '-o', videoPath, 'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.webm']);

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
    await page.getByRole('button', { name: /登录/ }).click();
    await page.waitForURL('**/#/overview');
    await page.goto('http://127.0.0.1:4175/#/sops');
    await page.getByRole('button', { name: '新建 SOP', exact: true }).click();
    const name = `QA-structure-only-groups-${Date.now()}`;
    await page.getByLabel('SOP 名称', { exact: true }).fill(name);
    await page.getByLabel('模拟时间起点').fill('2026-08-26T10:00');
    await page.getByLabel('节点 1 文字 1').fill('您好，這是為您整理的行程資料，歡迎告訴我們預計出發時間。');
    const group = n => page.getByRole('article', { name: `节点 ${n}`, exact: true });
    await group(1).getByRole('button', { name: '图片', exact: true }).click();
    const files = await page.evaluate(() => {
      const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
      const ctx = canvas.getContext('2d');
      const draw = label => { ctx.fillStyle = '#d9ece9'; ctx.fillRect(0, 0, 640, 360); ctx.fillStyle = '#075c58'; ctx.font = 'bold 30px sans-serif'; ctx.fillText('LOCAL SOP / TEST MEDIA', 45, 160); ctx.font = '18px sans-serif'; ctx.fillText(label, 45, 205); };
      draw('Structure test only - not a travel product image');
      const png = canvas.toDataURL('image/png').split(',')[1];
      return { png };
    });
    await page.getByLabel('节点 1 上传图片 2').setInputFiles({ name: 'local-preview.png', mimeType: 'image/png', buffer: Buffer.from(files.png, 'base64') });
    await page.getByText('local-preview.png', { exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('.sop-media-preview img')?.naturalWidth > 0);
    await group(1).getByRole('button', { name: '视频', exact: true }).click();
    await page.getByLabel('节点 1 上传视频 3').setInputFiles({ name: 'local-preview.webm', mimeType: 'video/webm', buffer: fs.readFileSync(videoPath) });
    await page.getByText('local-preview.webm', { exact: true }).waitFor();
    await page.locator('.sop-media-preview video').scrollIntoViewIfNeeded();
    await page.locator('.sop-media-preview video').evaluate(video => video.load());
    try {
      await page.waitForFunction(() => document.querySelector('.sop-media-preview video')?.readyState >= 1, undefined, { timeout: 10000 });
    } catch (error) {
      console.log(await page.locator('.sop-media-preview video').evaluate(video => ({ src: video.currentSrc, state: video.readyState, error: video.error?.message })));
      const media = await page.request.get(await page.locator('.sop-media-preview video').getAttribute('src'));
      console.log({ status: media.status(), type: media.headers()['content-type'], length: (await media.body()).length });
      await page.screenshot({ path: path.join(output, 'video-error.png') });
      throw error;
    }
    await page.locator('.sop-media-preview video').evaluate(async video => { video.muted = true; await video.play(); });
    await page.waitForFunction(() => document.querySelector('.sop-media-preview video')?.currentTime > 0.1);
    await page.locator('.sop-media-preview video').evaluate(video => video.pause());
    await page.getByLabel('上移内容 1-3').click();
    assert.deepStrictEqual(await group(1).locator('.sop-content-item').evaluateAll(xs => xs.map(x => x.dataset.contentType)), ['text', 'video', 'image']);
    await page.getByLabel('下移内容 1-2').click();
    for (let n = 2; n <= 5; n++) {
      await page.getByRole('button', { name: '添加节点', exact: true }).click();
      await page.getByLabel(`节点 ${n} 文字 1`).fill(`第 ${n} 组：已审核的跟进内容。`);
      if (n === 2) {
        await group(n).getByRole('button', { name: '相对分钟', exact: true }).click();
        await page.getByLabel('节点 2 延迟分钟').fill('20');
      } else if (n === 3 || n === 4) {
        await group(n).getByRole('button', { name: '第 N 天定时', exact: true }).click();
        await page.getByLabel(`节点 ${n} 第几天`).fill(String(n - 1));
        await page.getByLabel(`节点 ${n} 发送时刻`).fill('10:00');
      }
    }
    await page.waitForFunction(() => document.querySelectorAll('.sop-preview-timeline li').length === 5);
    assert((await page.locator('.sop-preview-timeline').innerText()).includes('08/28'));
    assert(!(await page.locator('.sop-preview-timeline').innerText()).includes('间隔小于联系人频控'));
    await page.locator('.sop-drawer-body').evaluate(el => { el.scrollTop = 0; });
    await page.screenshot({ path: path.join(output, 'editor-desktop.png') });
    await group(1).locator('.sop-content-list').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'group-media-desktop.png') });
    await group(3).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'calendar-day-desktop.png') });
    const responsePromise = page.waitForResponse(r => r.url().endsWith('/v1/sops') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '保存草稿', exact: true }).click();
    const response = await responsePromise;
    assert.equal(response.status(), 200);
    const saved = await response.json();
    assert.deepStrictEqual(saved.nodes[0].messages.map(x => x.content_type), ['text', 'image', 'video']);
    assert.equal(saved.nodes[2].day_number, 2);
    assert.equal(saved.nodes[3].day_number, 3);
    assert.equal(saved.nodes[4].basis, 'previous_node');
    assert.equal(saved.nodes[0].messages[1].media_name, 'local-preview.png');
    const row = page.locator('.sop-row').filter({ hasText: name });
    await row.getByTitle('编辑草稿', { exact: true }).click();
    await page.getByText('local-preview.png', { exact: true }).waitFor();
    await page.locator('.toast').waitFor({ state: 'hidden' });
    assert.equal(await page.getByLabel('节点 1 延迟分钟').inputValue(), '10');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(400);
    await page.locator('.sop-composer-layout').evaluate(el => { el.scrollTop = 0; });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert(await page.locator('.sop-composer').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    await page.screenshot({ path: path.join(output, 'editor-mobile.png') });
    await group(1).locator('.sop-content-list').scrollIntoViewIfNeeded();
    await page.locator('.sop-media-preview video').evaluate(async video => { video.muted = true; await video.play(); });
    await page.waitForFunction(() => document.querySelector('.sop-media-preview video')?.currentTime > 0.1);
    await page.locator('.sop-media-preview video').evaluate(video => video.pause());
    await page.screenshot({ path: path.join(output, 'group-media-mobile.png') });
    await page.locator('.sop-schedule-preview').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'timeline-mobile.png') });
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('dialog').count(), 0);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await row.getByTitle('发布草稿', { exact: true }).click();
    await row.getByText('运行中', { exact: true }).waitFor();
    await row.getByTitle('版本与执行明细', { exact: true }).click();
    await page.locator('.sop-version-detail summary').first().click();
    await page.getByRole('img', { name: '发布版本图片' }).waitFor();
    assert((await page.getByRole('dialog').innerText()).includes('入组后第 3 天'));
    await page.screenshot({ path: path.join(output, 'published-version.png') });
    await page.getByRole('button', { name: '关闭', exact: true }).last().click();
    await row.getByTitle('暂停策略', { exact: true }).click();
    await row.getByText('已暂停', { exact: true }).waitFor();
    assert.deepStrictEqual(errors, []);
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ passed: true, sop_id: saved.id, node_count: 5, content_count: 7, time_modes: 3, image_preview: true, video_preview: true, reordering: true, save_reload: true, published_version: true, mobile: true, page_errors: errors, actual_customer_sends: 0 }, null, 2));
    console.log('SOP groups desktop/mobile checks passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
