import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import test from 'node:test';
import { createClientId } from '../src/client-id.ts';

function useCrypto(t, value) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value });
  t.after(() => Object.defineProperty(globalThis, 'crypto', previous));
}

test('HTTP without randomUUID can create distinct valid message and row IDs', t => {
  const httpCrypto = {
    getRandomValues(array) {
      assert.equal(this, httpCrypto);
      return webcrypto.getRandomValues(array);
    },
  };
  useCrypto(t, httpCrypto);
  const ids = Array.from({ length: 512 }, () => createClientId());
  assert.equal(new Set(ids).size, ids.length);
  assert.ok(ids.every(id => /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/.test(id)));
});

test('HTTPS keeps the native UUID implementation and its receiver', t => {
  const id = '9ad21b22-65ec-46ab-80a6-dd0727e6b3b7';
  const httpsCrypto = { randomUUID() { assert.equal(this, httpsCrypto); return id; } };
  useCrypto(t, httpsCrypto);
  assert.equal(createClientId(), id);
});
