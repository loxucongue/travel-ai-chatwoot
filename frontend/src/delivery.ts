export const deliveryStates: Record<string, { label: string; tone: 'neutral' | 'green' | 'amber' | 'red' | 'blue' }> = {
  submitted: { label: '\u5df2\u63d0\u4ea4\uff0c\u7b49\u5f85\u786e\u8ba4', tone: 'amber' },
  sent: { label: '\u5df2\u53d1\u9001', tone: 'blue' },
  delivered: { label: '\u5df2\u9001\u8fbe', tone: 'green' },
  read: { label: '\u5df2\u8bfb', tone: 'green' },
  failed: { label: '\u53d1\u9001\u5931\u8d25', tone: 'red' },
  unknown: { label: '\u72b6\u6001\u672a\u77e5', tone: 'neutral' },
  submission_unknown: { label: '\u63d0\u4ea4\u7ed3\u679c\u672a\u77e5\uff0c\u5f85\u6838\u5b9e', tone: 'amber' },
  cancelled: { label: '\u5df2\u53d6\u6d88', tone: 'neutral' },
  verification_blocked: { label: '\u6821\u9a8c\u963b\u65ad\uff0c\u672a\u6267\u884c (no_action)', tone: 'red' },
  no_action: { label: '\u672a\u6267\u884c (no_action)', tone: 'neutral' },
  blocked: { label: '\u5df2\u963b\u65ad', tone: 'red' },
  simulated_delivered: { label: '\u5df2\u6a21\u62df\u9001\u8fbe', tone: 'blue' },
};

export function deliveryState(status?: unknown) {
  return typeof status === 'string' && status.trim()
    ? deliveryStates[status] ?? { label: status, tone: 'neutral' as const }
    : deliveryStates.unknown;
}

export function deliveryItem(attributes: unknown): Record<string, unknown> | null {
  if (!attributes || typeof attributes !== 'object') return null;
  const attrs = attributes as Record<string, unknown>;
  const item = [attrs.delivery_item, attrs._delivery_item]
    .find(value => value && typeof value === 'object' && !Array.isArray(value));
  if (!item) return null;
  const source = item as Record<string, unknown>;
  // Keep tracking hashes, asset bindings and other private metadata out of the display model.
  const normalized: Record<string, unknown> = {};
  for (const key of ['plan_version', 'group_key', 'item_id', 'status']) {
    if (typeof source[key] === 'string' || (typeof source[key] === 'number' && Number.isFinite(source[key]))) {
      normalized[key] = source[key];
    }
  }
  if (normalized.group_key == null && Array.isArray(source.group_keys)) {
    const groups = source.group_keys.filter((value): value is string => typeof value === 'string');
    if (groups.length) normalized.group_key = groups.join(' / ');
  }
  return normalized;
}

export function customerTranscriptMessage(message: { direction: string; status?: string; private?: boolean }) {
  if (message.private) return false;
  if (message.direction === 'incoming') return true;
  // Plan-item status is not a receipt: only the message's confirmed status counts.
  return message.direction === 'outgoing' && ['sent', 'delivered', 'read', 'simulated_delivered'].includes(message.status ?? '');
}

export type RuntimeEvidence = {
  status?: string;
  error_code?: string;
  decision: { action?: string; safety_flags?: string[] };
  trace: { fact_verification_passed?: boolean | null; fact_verification_mode?: string; question_coverage_passed?: boolean | null; skip_reason?: string | null;
    confirmation_questions?: string[]; unanswered_questions?: string[];
    handoff_task?: { id: string; status: string; reason: string; questions?: string[]; simulated: boolean };
    discussion_subject?: string;
    question_details?: { topic: string; question: string; reference_message_id?: string }[];
    knowledge_selection?: { selected_fact_ids?: string[]; rejected?: { fact_id: string; reason: string }[] };
  };
};

export function verificationBlocked(run: RuntimeEvidence) {
  return run.status === 'verification_blocked' || (run.decision.action === 'no_action' && (
    run.trace.fact_verification_passed === false || run.trace.question_coverage_passed === false ||
    [run.error_code, run.trace.skip_reason, ...(run.decision.safety_flags ?? [])]
      .some(value => typeof value === 'string' && /verification_failed|verification_blocked/.test(value))
  ));
}
