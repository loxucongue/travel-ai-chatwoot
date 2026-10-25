import test from 'node:test';
import assert from 'node:assert/strict';
import {followupCheckpoints, followupIntervals, validFollowupIntervals, configurationChanges} from '../src/pages/reception-config.ts';

test('advisor automatic followup switch is included when enabled and disabled', () => {
  const before={intake:{},reply:{},lead_capture:{},routing:{},handoff:{},silence:{advisor_reply_followup_enabled:false,daily_at:'09:00'}};
  const after=structuredClone(before);
  after.silence.advisor_reply_followup_enabled=true;
  assert.deepEqual(configurationChanges(before,after).silence,{advisor_reply_followup_enabled:true});
  assert.deepEqual(configurationChanges(after,before).silence,{advisor_reply_followup_enabled:false});
});

test('daily cadence roundtrip clears first-hour policy and preserves one-touch limit', () => {
  const before={intake:{},reply:{},lead_capture:{},routing:{},handoff:{},silence:{initial_checkpoints_minutes:[10,25,40,60],max_proactive_messages_per_day:12}};
  const after=structuredClone(before);
  Object.assign(after.silence,{daily_at:'09:00',initial_checkpoints_minutes:null,max_proactive_messages_per_day:1});
  assert.deepEqual(configurationChanges(before,after).silence,{daily_at:'09:00',initial_checkpoints_minutes:null,max_proactive_messages_per_day:1});
});

test('saving entry cadence preserves checkpoint, repeat interval and quiet hours', () => {
  const before={intake:{},reply:{},lead_capture:{},routing:{},handoff:{},silence:{initial_checkpoints_minutes:null,recurring_interval_minutes:180,active_start:'09:00',active_end:'00:00'}};
  const after=structuredClone(before);
  Object.assign(after.silence,{initial_checkpoints_minutes:[10,25,40,60],active_start:'07:00',active_end:'01:00',max_proactive_messages_per_day:12});
  assert.deepEqual(configurationChanges(before,after).silence,{initial_checkpoints_minutes:[10,25,40,60],active_start:'07:00',active_end:'01:00',max_proactive_messages_per_day:12});
});

test('six-hour first touch can be followed by later configured checkpoints', () => {
  const intervals=followupIntervals([360,1080]);
  assert.deepEqual(intervals,[360,720]);
  assert.equal(validFollowupIntervals(intervals),true);
  assert.equal(validFollowupIntervals([360,1020]),true);
  assert.equal(validFollowupIntervals([360,1021]),true);
  assert.equal(validFollowupIntervals([1440,1440,1440,1440,1440,1440,1440]),true);
  assert.equal(validFollowupIntervals([1440,1440,1440,1440,1440,1440,1441]),false);
  assert.equal(validFollowupIntervals(followupIntervals([360,360])),false);
  assert.equal(validFollowupIntervals([]),false);
  assert.equal(validFollowupIntervals([360,1.5]),false);
});

test('material rules and channel opt-in survive configuration patch', () => {
  const before={intake:{},reply:{},lead_capture:{},routing:{},handoff:{},silence:{},material_rules:[]};
  const after=structuredClone(before);
  after.routing.live_inbox_ids=[1,2];
  after.material_rules=[{id:'car',enabled:false}];
  assert.deepEqual(configurationChanges(before,after),{routing:{live_inbox_ids:[1,2]},material_rules:after.material_rules});
});

test('cumulative editor roundtrips without moving later followups on delete or edit', () => {
  const points=followupCheckpoints([1,4,10,45,120,180]);
  assert.deepEqual(points,[1,5,15,60,180,360]);
  points.splice(1,1);
  assert.deepEqual(followupCheckpoints(followupIntervals(points)),[1,15,60,180,360]);
  points[1]=20;
  assert.deepEqual(followupCheckpoints(followupIntervals(points)),[1,20,60,180,360]);
});
