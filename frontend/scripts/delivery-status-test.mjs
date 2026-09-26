import assert from 'node:assert/strict';
import { customerTranscriptMessage, deliveryItem, deliveryState, verificationBlocked } from '../src/delivery.ts';

for (const status of ['submitted', 'sent', 'delivered', 'read', 'failed', 'unknown', 'cancelled', 'verification_blocked', 'no_action', 'submission_unknown', 'simulated_delivered']) {
  assert.ok(deliveryState(status).label);
}
assert.equal(new Set(['submitted', 'sent', 'delivered', 'read', 'failed', 'unknown', 'cancelled', 'verification_blocked', 'no_action'].map(status => deliveryState(status).label)).size, 9);
for (const absent of [undefined, null, '', ' ', 0, {}]) {
  assert.deepEqual(deliveryState(absent), deliveryState('unknown'));
}
assert.equal(deliveryState('future_status').label, 'future_status');
assert.equal(deliveryState('submitted').tone, 'amber');
assert.notEqual(deliveryState('simulated_delivered').label, deliveryState('delivered').label);
for (const attrs of [undefined, null, {}, { delivery_item: [] }, { delivery_item: 'bad' }]) assert.equal(deliveryItem(attrs), null);
const item = { plan_version: 0, group_key: 'group-1', item_id: 'item-1', status: 'cancelled' };
assert.deepEqual(deliveryItem({ delivery_item: item }), item);
assert.deepEqual(deliveryItem({ _delivery_item: { ...item, content_hash: 'private', asset_hash: 'secret' } }), item);
assert.deepEqual(deliveryItem({ delivery_item: item, _delivery_item: { status: 'failed' } }), item);
assert.deepEqual(deliveryItem({ delivery_item: null, _delivery_item: item }), item);
assert.deepEqual(deliveryItem({ _delivery_item: { group_keys: ['first', {}, 'second'], item_id: 'x', snapshot_digest: 'secret' } }), { group_key: 'first / second', item_id: 'x' });
assert.deepEqual(deliveryItem({ _delivery_item: { plan_version: {}, status: [], item_id: NaN } }), {});
assert.equal(verificationBlocked({ status: 'completed', decision: { action: 'reply' }, trace: {} }), false);
assert.equal(verificationBlocked({ decision: { action: 'no_action' }, trace: {} }), false);
assert.equal(verificationBlocked({ decision: { action: 'no_action' }, trace: { fact_verification_passed: false } }), true);
assert.equal(verificationBlocked({ decision: { action: 'no_action', safety_flags: ['silence_verification_failed_no_action'] }, trace: {} }), true);
assert.equal(verificationBlocked({ status: 'verification_blocked', decision: {}, trace: {} }), true);
console.log('Delivery status tests passed');
for (const status of ['draft', 'scheduled', 'pending', 'cancelled', 'blocked', 'verification_blocked', 'failed', 'submitted', 'submission_unknown', 'unknown', 'no_action', 'already_provided', undefined]) {
  assert.equal(customerTranscriptMessage({ direction: 'outgoing', status }), false, `Customer transcript must exclude ${status}`);
}
for (const status of ['sent', 'delivered', 'read', 'simulated_delivered']) {
  assert.equal(customerTranscriptMessage({ direction: 'outgoing', status }), true);
  assert.equal(customerTranscriptMessage({ direction: 'outgoing', status, private: true }), false);
}
assert.equal(customerTranscriptMessage({ direction: 'incoming' }), true);
assert.equal(customerTranscriptMessage({ direction: 'incoming', private: true }), false);
assert.equal(customerTranscriptMessage({ direction: 'outgoing', status: 'draft', ...{ delivery_item: { status: 'delivered' } } }), false);
console.log('Customer transcript visibility tests passed');
