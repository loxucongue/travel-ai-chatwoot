async (page) => {
  const results = [];
  await page.goto('http://127.0.0.1:5190/#/conversations');
  await page.locator('.chat-identity strong').waitFor();
  for (const width of [1440, 1024, 768, 430, 390, 320]) {
    await page.setViewportSize({ width, height: width > 700 ? 1000 : 844 });
    await page.locator('.chat-header').scrollIntoViewIfNeeded();
    const result = await page.evaluate(() => {
      const rect = selector => document.querySelector(selector).getBoundingClientRect();
      const identity = rect('.chat-identity'), actions = rect('.chat-actions');
      const title = rect('.chat-identity strong'), header = rect('.chat-header');
      const left = rect('.topbar-left'), right = rect('.topbar-actions');
      return { width: innerWidth, documentWidth: document.documentElement.scrollWidth,
        titleOverlapsActions: title.right > actions.left, identityOverlapsActions: identity.right > actions.left,
        actionsOverflow: actions.right > header.right, topbarOverlap: left.right > right.left,
        topbarOverflow: right.right > innerWidth,
        fullNameTooltip: document.querySelector('.chat-identity strong').title === 'ROUTE MOCK delivery QA' };
    });
    if (result.titleOverlapsActions || result.identityOverlapsActions || result.actionsOverflow || result.topbarOverlap || result.topbarOverflow || result.documentWidth > width || !result.fullNameTooltip) throw new Error(JSON.stringify(result));
    results.push(result);
    if ([1440, 390, 320].includes(width)) await page.screenshot({ path: `output/playwright/delivery-responsive-${width}.png` });
  }
  await page.goto('http://127.0.0.1:5190/#/sops');
  await page.getByRole('button', { name: '版本与执行明细' }).waitFor();
  const sopWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  if (sopWidth > 320) throw new Error(`SOP page overflows: ${sopWidth}`);
  await page.screenshot({ path: 'output/playwright/delivery-topbar-320.png' });
  const log = await page.evaluate(async () => (await fetch('/delivery-qa-report')).json());
  if (log.errors.length) throw new Error(JSON.stringify(log.errors));
  return { results, sopWidth, errors: log.errors };
}
