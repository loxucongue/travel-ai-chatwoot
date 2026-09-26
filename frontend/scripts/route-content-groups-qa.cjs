const { chromium } = require('C:/Users/24159/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '../..');
const output = path.join(root, 'output/playwright/route-content-groups');
const source = JSON.parse(fs.readFileSync(path.join(root, 'data/knowledge/china2go/route-packages/peach-9d-2027/route-package.json'), 'utf8'));
const product = {
  ...source, assets: [], default_sop: { nodes: [] }, versions: [],
  readiness: { assets_ready: 0, assets_total: 0, missing_assets: [], ai_reply_ready: true, sop_ready: true },
  content_groups: Object.entries(source.content_groups).map(([key, group]) => ({ key, sequence: null, initial_delivery: false, delivery_mode: 'text_only', ...group })),
};
const config = {
  schema_version: 5, reply: {}, routing: { enabled_route_variants: [product.route_variant] },
  business_rules: [], silence: {}, stage_journey: {},
};

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const width of [1440, 390]) {
      let saved = structuredClone(product);
      const writes = [];
      const errors = [];
      const page = await browser.newPage({ viewport: { width, height: 1000 } });
      async function publish() {
        const refreshed = page.waitForResponse(r => r.url().endsWith('/automation/route-products') && r.request().method() === 'GET');
        await page.getByRole('button', { name: '校验并发布', exact: true }).click();
        await refreshed;
        await page.getByRole('button', { name: '校验并发布', exact: true }).waitFor();
      }
      page.on('pageerror', error => errors.push(error.message));
      // All API traffic is isolated; this test never writes to the running backend.
      await page.route('**/v1/**', async route => {
        const request = route.request();
        const endpoint = new URL(request.url()).pathname.replace(/^.*\/v1/, '');
        let body = {};
        if (endpoint === '/auth/me') body = { id: 1, display_name: 'QA', email: 'qa@example.test', roles: ['admin'], permissions: [], must_change_password: false };
        else if (endpoint === '/auth/csrf') body = { csrf_token: 'test' };
        else if (endpoint === '/notifications') body = { items: [], unread: 0 };
        else if (endpoint === '/automation/reception-config') body = { config };
        else if (endpoint === '/automation/route-products') body = { items: [saved], outbound: false };
        else if (endpoint.endsWith('/content') && request.method() === 'PUT') {
          const draft = request.postDataJSON();
          if (draft.base_package_version !== saved.package_version) {
            await route.fulfill({ status: 409, json: { error: { code: 'route_content_version_conflict', message: '线路已有新版本，请刷新后重新编辑。' } } });
            return;
          }
          if (saved.content_groups.some(g => g.key === 'read_check') && !draft.content_groups.some(g => g.key === 'read_check')) {
            assert.deepEqual(draft.delete_sop_group_keys, ['read_check']);
          }
          writes.push(draft);
          saved = { ...saved, ...draft, package_version: `qa-version-${writes.length}` };
          body = { draft: { package_version: saved.package_version }, outbound: false };
        } else if (!['GET', 'OPTIONS'].includes(request.method())) throw new Error(`Unexpected write: ${endpoint}`);
        await route.fulfill({ json: body });
      });
      await page.goto(`${process.env.ROUTE_QA_URL || 'http://127.0.0.1:4186'}/#/products`);
      await page.getByRole('button', { name: '配置', exact: true }).first().click();
      await page.getByRole('button', { name: '线路资料', exact: true }).click();
      await page.getByRole('button', { name: '新增内容组', exact: true }).click();
      await page.getByLabel('名称与用途', { exact: true }).fill('行前准备');
      await page.getByLabel('参考内容', { exact: true }).fill('請攜帶保暖衣物。');
      await page.screenshot({ path: path.join(output, `add-${width}.png`), fullPage: true });
      const form = page.locator('form').filter({ has: page.getByLabel('名称与用途', { exact: true }) });
      assert(await form.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
      await page.getByRole('button', { name: '添加', exact: true }).click();
      await page.getByRole('button', { name: '完成编辑', exact: true }).click();
      await page.getByRole('button', { name: '接待主线', exact: true }).click();
      await page.getByTitle('加入主线：行前准备', { exact: true }).click();
      await publish();
      assert.equal(writes.length, 1);
      const added = writes[0].content_groups.find(g => g.purpose === '行前准备');
      assert.match(added.key, /^operator_[a-f0-9]{32}$/);
      assert(writes[0].content_sequence.includes(added.key));
      await page.getByRole('button', { name: '线路资料', exact: true }).click();
      await page.locator('.route-module-card').filter({ hasText: '行前准备' }).click();
      await page.getByLabel('名称与用途', { exact: true }).fill('行前衣物');
      await page.getByRole('button', { name: '完成编辑', exact: true }).click();
      await publish();
      assert.equal(writes[1].content_groups.find(g => g.purpose === '行前衣物').key, added.key);
      await page.locator('.route-module-card').filter({ hasText: '行前衣物' }).click();
      page.once('dialog', dialog => dialog.dismiss());
      await page.getByRole('button', { name: '删除内容组', exact: true }).click();
      assert(await page.getByRole('dialog').isVisible());
      page.once('dialog', dialog => dialog.accept());
      await page.getByRole('button', { name: '删除内容组', exact: true }).click();
      await publish();
      assert(!writes[2].content_groups.some(g => g.key === added.key));
      assert(!writes[2].content_sequence.includes(added.key));
      await page.locator('.route-module-card').filter({ hasText: '沉默承接' }).click();
      const consent = page.getByRole('checkbox', { name: '同时删除该组同名的源 SOP 节点', exact: true });
      assert.equal(await consent.isChecked(), false);
      await consent.check();
      await page.screenshot({ path: path.join(output, `source-delete-${width}.png`) });
      page.once('dialog', dialog => dialog.dismiss());
      await page.getByRole('button', { name: '删除内容组', exact: true }).click();
      assert(await page.getByRole('dialog').isVisible());
      page.once('dialog', async dialog => {
        assert(dialog.message().includes('同时删除该组同名的源 SOP 节点'));
        assert(dialog.message().includes('旧版本快照不变'));
        await dialog.accept();
      });
      await page.getByRole('button', { name: '删除内容组', exact: true }).click();
      await publish();
      assert(!writes[3].content_groups.some(g => g.key === 'read_check'));
      assert(!writes[3].content_sequence.includes('read_check'));
      assert.deepEqual(writes[3].delete_sop_group_keys, ['read_check']);
      await page.locator('.route-module-card').filter({ has: page.locator('strong', { hasText: /^住宿$/ }) }).click();
      assert.equal(await consent.isChecked(), false);
      await consent.check();
      await page.getByRole('button', { name: '删除内容组', exact: true }).click();
      await page.getByRole('dialog').getByRole('alert').filter({ hasText: '策略' }).waitFor();
      await page.getByRole('button', { name: '完成编辑', exact: true }).click();
      const answer = saved.fixed_answers[0];
      const index = saved.content_groups.findIndex(g => g.key === answer.content_group_key);
      await page.locator('.route-module-card').filter({ hasText: saved.content_groups[index].purpose }).first().click();
      await page.getByRole('button', { name: '删除内容组', exact: true }).click();
      await page.getByRole('dialog').getByRole('alert').filter({ hasText: '固定回答' }).waitFor();
      await page.screenshot({ path: path.join(output, `dependency-${width}.png`), fullPage: true });
      await page.getByLabel('名称与用途', { exact: true }).fill('发布后的普通修改');
      await page.getByRole('button', { name: '完成编辑', exact: true }).click();
      await publish();
      assert.equal(writes.length, 5);
      assert.deepEqual(writes[4].delete_sop_group_keys ?? [], []);
      await page.locator('.route-module-card').filter({ hasText: '发布后的普通修改' }).click();
      await page.getByLabel('名称与用途', { exact: true }).fill('未发布的本地修改');
      await page.getByRole('button', { name: '完成编辑', exact: true }).click();
      saved.package_version = 'external-new-version';
      const rejected = page.waitForResponse(r => r.status() === 409);
      await page.getByRole('button', { name: '校验并发布', exact: true }).click();
      await rejected;
      await page.getByText('线路已有新版本，请刷新后重新编辑。', { exact: true }).waitFor();
      assert.equal(writes.length, 5);
      assert(await page.locator('.route-module-card').filter({ hasText: '未发布的本地修改' }).isVisible());
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}px: add, mainline, stable key, source-SOP consent/cancel/cleanup, policy and fixed-answer guards, repeated publish, stale-version rejection`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
