async (page) => {
  const statuses = ['submitted', 'sent', 'delivered', 'read', 'failed', 'unknown', 'cancelled', 'verification_blocked', 'no_action', 'submission_unknown', undefined, 'draft', 'scheduled', 'blocked', 'simulated_delivered'];
  const now = '2026-09-13T08:00:00Z';
  const messages = statuses.map((status, index) => ({
    id: index + 1, direction: 'outgoing', private: false, attribution: 'ai', source: 'ai',
    content_type: 'text', content: `ROUTE MOCK ${index + 1}: ${status ?? 'missing status'}`, status,
    created_at: now, timeline_sequence: index,
    content_attributes: index === 10 ? {} : {
      [index % 2 ? '_delivery_item' : 'delivery_item']: {
        plan_version: 0, item_id: index === 6 ? 'item-very-long-'.repeat(12) : `item-${index + 1}`,
        ...(index % 2 ? { group_keys: ['route-introduction', 'next-step'] } : { group_key: 'route-introduction' }),
        ...(index === 1 ? {} : { status }), content_hash: 'PRIVATE_SENTINEL', snapshot_digest: 'PRIVATE_SENTINEL',
      }, _delivery_asset_bindings: 'PRIVATE_SENTINEL',
    },
  }));
  messages[11].created_at = '2026-09-14T08:00:00Z';
  messages[11]._delivery_item = { plan_version: 'future-v2', group_keys: ['future-group'], item_id: 'future-item', status: 'draft', content_hash: 'PRIVATE_SENTINEL' };
  messages[11].content_attributes = {};
  messages[14].delivery_item = { plan_version: 'confirmed-v1', group_key: 'confirmed-group', item_id: 'confirmed-item', status: 'simulated_delivered' };
  messages[14].content_attributes = {};
  messages.push({ id: 16, direction: 'incoming', content: 'ROUTE MOCK customer message', created_at: now });
  const runs = [{ id: 1, session_id: 42, module: 'reply', generation: 1, status: 'completed', created_at: now,
    decision: { action: 'no_action', safety_flags: ['silence_verification_failed_no_action'] },
    trace: { total_ms: 120, fact_verification_passed: false, question_coverage_passed: false, skip_reason: 'fact_verification_failed_no_action' } },
    { id: 2, session_id: 42, module: 'reply', generation: 1, status: 'completed', created_at: now, decision: { action: 'reply' }, trace: {} }];
  const conversation = { id: 42, name: 'ROUTE MOCK delivery QA', channel: 'facebook', inbox: 'ROUTE MOCK', inbox_id: 1,
    last_message: 'ROUTE MOCK status coverage', ai_state: 'AI_PAUSED_GLOBAL', ai_reason: '', labels: [], can_reply: false,
    status: 'open', ai_mode: 'disabled', ai_mode_source: 'conversation', ai_label_present: false, ai_sync_status: 'synced',
    version: 1, updated_at: now, chatwoot_url: '', contact: { name: 'ROUTE MOCK', pii_masked: true } };
  const data = {
    '/auth/me': { id: 1, display_name: 'ROUTE MOCK QA', email: 'mock@example.invalid', roles: ['admin'], permissions: [], must_change_password: false },
    '/auth/csrf': { csrf_token: 'mock-only' }, '/notifications': { items: [], unread: 0 },
    '/settings/global-message-sending': { enabled: false, effective_enabled: false },
    '/settings/inboxes': [{ id: 1, name: 'ROUTE MOCK inbox', chatwoot_inbox_id: 1 }],
    '/playground/sessions': { items: [{ id: 42, mode: 'journey', status: 'running', created_at: now }] },
    '/playground/sessions/42': { id: 42, generation: 1, environment: 'playground', mode: 'journey', virtual_now: now,
      pending: false, outbound: false, controls: { human: false, can_reply: false, ai_enabled: false },
      simulation: { status: 'running', speed_multiplier: 1 }, memory: {}, messages, runs,
      jobs: [{ id: 1, node_key: 'wakeup_0', status: 'verification_blocked', reason: 'fact_verification_failed_no_action', scheduled_at: now }] },
    '/conversations/filters': { inboxes: [], labels: [], ai_states: [] },
    '/conversations': { items: [conversation], total: 1, page: 1, page_size: 25 },
    '/conversations/42': conversation, '/conversations/42/messages': { items: messages },
    '/conversations/42/controls': { assignee: null, team: null, agents: [], labels: [], conversation_labels: [], ai_mode: 'disabled', ai_label_present: false, ai_sync_status: 'synced', can_create_labels: false },
    '/automation/reply-policy': { version: 1, enabled: false, merge_wait_seconds: 1, merge_max_seconds: 5, backlog_seconds: 30, inbox_binding_id: null },
    '/automation/runs': { items: runs, total: runs.length },
    '/sops': { items: [{ id: 42, name: 'ROUTE MOCK delivery QA', description: '', status: 'paused', version: 1, nodes: [],
      trigger_type: 'manual', trigger_labels: [], exit_labels: [], inbox_ids: [], test_conversation_ids: [], updated_at: now }] },
    '/sops/options': { live_test_enabled: false, live_test_conversation_ids: [] },
    '/sops/42/versions': { items: [] }, '/sops/42/preview': { total: 0, items: [] },
    '/sops/42/executions': { items: statuses.map((status, index) => ({ id: index, node_key: `ROUTE MOCK ${index}`, status: status ?? 'unknown',
      environment: 'playground', scheduled_at: now, delivery_items: [{ key: `item-${index}`, status }] })) },
  };
  const errors = [], requests = [];
  page.on('pageerror', error => errors.push(String(error)));
  page.on('console', message => { if (['error', 'warning'].includes(message.type())) errors.push(`${message.type()}: ${message.text()}`); });
  page.on('requestfailed', request => errors.push(`requestfailed: ${request.url()} ${request.failure()?.errorText}`));
  await page.route('**/v1/**', async route => {
    const path = route.request().url().split('/v1')[1].split('?')[0];
    requests.push({ path, method: route.request().method() });
    const body = data[path];
    if (!body || !['GET', 'OPTIONS'].includes(route.request().method())) errors.push(`Unexpected mock request: ${route.request().method()} ${path}`);
    await route.fulfill({ status: body ? 200 : 404, contentType: 'application/json', body: JSON.stringify(body ?? { error: { message: 'Unmocked route' } }) });
  });
  await page.route('**/delivery-qa-report', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ errors, requests }) }));
  await page.route('**/delivery-qa-transition', async route => {
    messages[11].status = messages[11].status === 'draft' ? 'simulated_delivered' : 'draft';
    await route.fulfill({ contentType: 'application/json', body: '{}' });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('http://127.0.0.1:5190/#/playground?session=42');
}
