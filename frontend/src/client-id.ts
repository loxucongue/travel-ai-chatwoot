/** IDs for message deduplication and editable rows, including HTTP deployments. */
export function createClientId(): string {
  const random = globalThis.crypto;
  if (typeof random.randomUUID === 'function') return random.randomUUID();

  // getRandomValues is also available on HTTP; retain cryptographic randomness.
  const bytes = random.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0'));
  return [hex.slice(0, 4), hex.slice(4, 6), hex.slice(6, 8), hex.slice(8, 10), hex.slice(10)]
    .map(part => part.join('')).join('-');
}
