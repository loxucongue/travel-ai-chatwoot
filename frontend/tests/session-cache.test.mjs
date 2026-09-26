import assert from 'node:assert/strict';
import test from 'node:test';
import { QueryObserver } from '@tanstack/react-query';
import { createSessionQueryClient, replaceSessionQueryClient } from '../src/session-cache.ts';

test('switching identities removes fresh restricted data and isolates late callbacks', async () => {
  const previous = createSessionQueryClient();
  const key = ['conversations', '', '', '', '0', 1];
  previous.setQueryData(key, { items: [{ id: 100, content: 'restricted' }] });
  let resolve;
  const pending = previous.fetchQuery({
    queryKey: ['messages', 100],
    queryFn: () => new Promise(done => { resolve = done; }),
  }).catch(() => undefined);
  const next = replaceSessionQueryClient(previous);
  assert.equal(previous.getQueryData(key), undefined);
  const observer = new QueryObserver(next, { queryKey: key, queryFn: async () => ({ items: [] }) });
  assert.equal(observer.getCurrentResult().data, undefined);
  resolve({ content: 'late restricted response' });
  await pending;
  previous.setQueryData(key, { items: [{ content: 'late mutation callback' }] });
  assert.equal(next.getQueryData(key), undefined);
  assert.equal(next.getQueryData(['messages', 100]), undefined);
  next.clear();
  previous.clear();
});
