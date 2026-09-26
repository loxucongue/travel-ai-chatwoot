async (page) => {
  const results = [];
  async function check(name, selector) {
    const result = await page.locator(selector).evaluateAll(elements => {
      const visible = elements.filter(e => e.getClientRects().length);
      const overflow = visible.filter(e => e.scrollWidth > e.clientWidth + 1 || e.getBoundingClientRect().right > innerWidth + 1 || e.getBoundingClientRect().left < -1);
      return { count: visible.length, overflow: overflow.map(e => e.textContent), privateLeak: document.body.textContent.includes('PRIVATE_SENTINEL'), viewport: innerWidth, documentWidth: document.documentElement.scrollWidth };
    });
    if (!result.count || result.overflow.length || result.privateLeak) throw new Error(`${name}: ${JSON.stringify(result)}`);
    results.push({ name, ...result });
  }
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    const size = viewport.width === 1440 ? 'desktop' : 'mobile';
    await page.setViewportSize(viewport);
    await page.goto('http://127.0.0.1:5190/#/playground?session=42');
    await page.locator('.rehearsal-message .customer-delivery-status').first().waitFor({ state: 'visible' });
    await check(`playground-${size}`, '.rehearsal-message .customer-delivery-status, .rehearsal-message .customer-delivery-status .badge');
    const transcript = await page.locator('.ai-chat-transcript').innerText();
    for (const hidden of ['submitted', 'draft', 'scheduled', 'cancelled', 'blocked', 'failed', 'unknown', 'missing status', 'no_action']) {
      if (transcript.includes(`: ${hidden}`)) throw new Error(`Unsent record visible: ${hidden}`);
    }
    if (await page.locator('.rehearsal-message').count() !== 5 || !transcript.includes('ROUTE MOCK customer message')) throw new Error('Confirmed transcript regression');
    for (const field of ['plan_version', 'group_key', 'item_id', 'delivery_item', 'confirmed-v1']) {
      if (transcript.includes(field)) throw new Error(`Technical metadata exposed in customer transcript: ${field}`);
    }
    await page.locator('.ai-chat-scroll').evaluate(e => { e.scrollTop = 0; });
    await page.screenshot({ path: `output/playwright/delivery-playground-${size}.png` });
    await page.locator('.ai-chat-topbar-actions button').filter({ hasText: '接待状态' }).click();
    const audit = page.locator('.delivery-audit details');
    if (await audit.count() !== 15) throw new Error('Outgoing audit records missing');
    await audit.nth(14).locator('summary').click();
    const confirmedAudit = await audit.nth(14).innerText();
    for (const field of ['plan_version', 'group_key', 'item_id', 'confirmed-v1']) {
      if (!confirmedAudit.includes(field)) throw new Error(`Inspector metadata missing: ${field}`);
    }
    await audit.nth(14).locator('summary').click();
    await audit.nth(11).locator('summary').click();
    if (!(await audit.nth(11).innerText()).includes('future-v2')) throw new Error('Private record metadata missing');
    await audit.nth(11).evaluate(e => e.scrollIntoView({ block: 'center' }));
    await check(`audit-${size}`, '.delivery-audit details[open] .delivery-details, .delivery-audit details[open] .badge');
    await page.screenshot({ path: `output/playwright/delivery-audit-${size}.png` });
    await page.locator('.ai-chat-details section:not(.delivery-audit) .delivery-details').scrollIntoViewIfNeeded();
    await check(`runtime-${size}`, '.ai-chat-details section:not(.delivery-audit) .delivery-details, .ai-chat-details section:not(.delivery-audit) .delivery-details .badge');
    await page.screenshot({ path: `output/playwright/delivery-runtime-${size}.png` });
    await page.evaluate(async () => { await fetch('/delivery-qa-transition'); });
    await page.waitForFunction(() => document.querySelectorAll('.rehearsal-message').length === 6);
    await page.evaluate(async () => { await fetch('/delivery-qa-transition'); });
    await page.waitForFunction(() => document.querySelectorAll('.rehearsal-message').length === 5);

    await page.goto('http://127.0.0.1:5190/#/conversations');
    await page.locator('.message-row .delivery-details').first().waitFor({ state: 'visible' });
    await check(`conversations-${size}`, '.message-row .delivery-details, .message-row .delivery-details .badge');
    const operational = await page.locator('.message-row').allTextContents();
    if (!operational.some(text => text.includes('failed') && text.includes('发送失败'))) throw new Error('Operational failure indicator hidden');
    await page.locator('.message-row').nth(size === 'mobile' ? 6 : 0).evaluate(e => e.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: `output/playwright/delivery-conversations-${size}.png` });

    await page.goto('http://127.0.0.1:5190/#/reply-policy');
    await page.locator('.automation-run-list summary').first().click();
    await page.locator('.automation-run-list summary').nth(1).click();
    await check(`operations-${size}`, '.run-expanded .delivery-details, .run-expanded .delivery-details .badge');
    await page.locator('.automation-run-list').scrollIntoViewIfNeeded();
    await page.screenshot({ path: `output/playwright/delivery-operations-${size}.png` });

    await page.goto('http://127.0.0.1:5190/#/sops');
    await page.getByRole('button', { name: '版本与执行明细' }).click();
    await page.getByRole('tab', { name: '执行明细' }).click();
    await page.locator('[role=dialog] .automation-policy-row').first().waitFor({ state: 'visible' });
    await check(`sop-${size}`, '[role=dialog] .automation-policy-row .badge');
    await page.screenshot({ path: `output/playwright/delivery-sop-${size}.png` });
    await page.locator('[role=dialog] .automation-policy-row').nth(6).evaluate(e => e.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: `output/playwright/delivery-sop-${size}-blocked.png` });
  }
  const log = await page.evaluate(async () => (await fetch('/delivery-qa-report')).json());
  if (log.errors.length || log.requests.some(request => request.method !== 'GET')) throw new Error(JSON.stringify(log));
  return { source: 'Browser route mocks only; no backend writes', results, errors: log.errors, requestCount: log.requests.length, methods: ['GET'] };
}
