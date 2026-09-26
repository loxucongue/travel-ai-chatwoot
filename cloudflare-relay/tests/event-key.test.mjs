import assert from 'node:assert/strict';
import { test } from 'node:test';
import { eventSuffix } from '../src/event-key.ts';

const body = labels => new TextEncoder().encode(JSON.stringify({ id: 26, labels })).buffer;
for (const event of ['conversation_updated', 'conversation_status_changed', 'contact_updated', 'message_updated']) {
  test(`${event}: repeated delivery dedupes, changed state survives`, async () => {
    const enabled = await eventSuffix(event, body(['ai']));
    const disabled = await eventSuffix(event, body([]));
    assert.notEqual(enabled, disabled);
    assert.equal(enabled, await eventSuffix(event, body(['ai'])));
  });
}
test('message_created remains resource-id idempotent', async () => {
  assert.equal(await eventSuffix('message_created', body(['ai'])), '');
});
