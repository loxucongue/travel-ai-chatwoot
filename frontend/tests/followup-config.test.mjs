import test from 'node:test';
import assert from 'node:assert/strict';
import {followupCheckpoints, followupIntervals, validFollowupIntervals} from '../src/pages/reception-config.ts';

test('six-hour first touch can be followed by later configured checkpoints', () => {
  const intervals=followupIntervals([360,1080]);
  assert.deepEqual(intervals,[360,720]);
  assert.equal(validFollowupIntervals(intervals),true);
  assert.equal(validFollowupIntervals([360,1020]),true);
  assert.equal(validFollowupIntervals([360,1021]),false);
  assert.equal(validFollowupIntervals(followupIntervals([360,360])),false);
  assert.equal(validFollowupIntervals([]),false);
  assert.equal(validFollowupIntervals([360,1.5]),false);
});

test('cumulative editor roundtrips without moving later followups on delete or edit', () => {
  const points=followupCheckpoints([1,4,10,45,120,180]);
  assert.deepEqual(points,[1,5,15,60,180,360]);
  points.splice(1,1);
  assert.deepEqual(followupCheckpoints(followupIntervals(points)),[1,15,60,180,360]);
  points[1]=20;
  assert.deepEqual(followupCheckpoints(followupIntervals(points)),[1,20,60,180,360]);
});
