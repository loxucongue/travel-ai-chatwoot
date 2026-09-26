const MUTABLE_EVENTS = new Set([
  'message_updated',
  'conversation_updated',
  'conversation_status_changed',
  'contact_updated',
]);

export async function eventSuffix(eventName: string, body: ArrayBuffer): Promise<string> {
  if (!MUTABLE_EVENTS.has(eventName)) return '';
  const hash = await crypto.subtle.digest('SHA-256', body);
  const hex = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
  return `:${hex.slice(0, 16)}`;
}
