import test from 'node:test';
import assert from 'node:assert/strict';
import { localDay, validRange } from '../src/pages/bi-types.ts';
test('BI dates use UTC+8 across midnight and year boundary', () => {
  assert.equal(localDay(0,new Date('2026-12-31T16:00:00Z')),'2027-01-01');
  assert.equal(localDay(-1,new Date('2026-12-31T16:00:00Z')),'2026-12-31');
  assert.equal(localDay(0,new Date('2026-10-30T15:59:59Z')),'2026-10-30');
});
test('custom date range rejects invalid dates, reversed ranges, future and excessive span', () => {
  const today='2026-10-30';
  for (const [a,b] of [['2026-02-30',today],[today,'2026-10-29'],[today,'2026-10-31'],['2024-01-01',today],['',today]]) assert.equal(validRange(a,b,today),false);
  assert.equal(validRange('2026-10-01',today,today),true);
});
