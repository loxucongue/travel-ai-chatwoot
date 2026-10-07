import test from 'node:test';
import assert from 'node:assert/strict';
import {followupCheckpoints, followupIntervals} from '../src/pages/reception-config.ts';

test('cumulative editor roundtrips without moving later followups on delete or edit', () => {
  const points=followupCheckpoints([1,4,10,45,120,180]);
  assert.deepEqual(points,[1,5,15,60,180,360]);
  points.splice(1,1);
  assert.deepEqual(followupCheckpoints(followupIntervals(points)),[1,15,60,180,360]);
  points[1]=20;
  assert.deepEqual(followupCheckpoints(followupIntervals(points)),[1,20,60,180,360]);
});
