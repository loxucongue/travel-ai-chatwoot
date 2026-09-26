export type ProductAsset = {
  key: string; id: number | null; display_name: string; media_type: string;
  usage: string; content_group_key: string; available: boolean;
  media_id: number | null; preview_url: string | null;
  live_approved?: boolean; review_state?: string; file_hash?: string;
  what_it_shows: string; feature_points: string[]; customer_value: string;
  recommended_caption: string; avoid_claims: string[];
};

export type ContentGroup = {
  key: string; purpose: string; approved_text: string; asset_keys: string[];
  evidence_refs: string[]; sequence: number | null; initial_delivery: boolean;
  delivery_mode: 'text_only' | 'assets_only' | 'text_then_assets' | 'assets_then_text';
};

export type FixedAnswer = {
  id: string; name: string; status: 'active' | 'pending_review' | 'disabled'; priority: number; topics: string[];
  party_size_min: number | null; party_size_max: number | null;
  content_group_key: string; answer_text: string; fact_ids: string[]; asset_ids: string[];
  source_ref: string; answer_origin: 'website_verbatim' | 'operator_approved'; positive_examples: string[];
  negative_examples: string[];
};

export type ProductFact = { id: string; text: string; source_ref: string };
export type SopNode = { key: string; delay_minutes?: number; delay_seconds?: number; delivery_interval_seconds?: number };
export type ProductVersion = {
  version: string; status: 'current' | 'history'; summary: string;
  created_at: string | null; user_id: number | null;
};

export type RouteProduct = {
  route_variant: string; name: string; selection_title: string;
  package_version: string; knowledge_version: string;
  default_entry_message: string; ai_guidance: string; match_keywords: string[]; required_slots: string[];
  initial_delivery_interval_seconds: number;
  source: { url?: string };
  knowledge_facts: ProductFact[];
  content_sequence: string[]; content_groups: ContentGroup[]; fixed_answers: FixedAnswer[];
  journey_policy: { policy_version: string };
  assets: ProductAsset[];
  default_sop: { nodes: SopNode[] };
  versions: ProductVersion[];
  readiness: {
    assets_ready: number; assets_total: number; missing_assets: string[];
    ai_reply_ready: boolean; sop_ready: boolean; default_sop_published: boolean;
  };
};

export type BusinessRule = {
  id: string; name: string; enabled: boolean; condition: string;
  action: 'handoff' | 'request_contact' | 'recommend_routes' | 'continue_ai' | 'stop_ai';
  guidance: string;
  trigger?: 'semantic' | 'outside_catalog';
  system_key: 'large_group' | 'captured_contact' | 'explicit_human' | 'service_dispute' | 'attachment_review' | null;
};

export type OpeningItem = {
  key: string;
  content_type: 'text' | 'image' | 'video';
  content: string;
  media_id?: number | null;
  media_hash?: string;
  media_name?: string;
};

export type ReceptionConfig = {
  schema_version: number;
  reply: { opening_message: string; opening_messages: string[]; opening_items: OpeningItem[]; opening_interval_seconds: number; goal: string; tone: 'friendly_professional' | 'concise' | 'warm'; tone_guidance: string; max_characters: number; max_images_per_turn: number; custom_guidance: string };
  profile_fields: string[];
  lead_capture: { enabled: boolean; channels: ('LINE' | '微信' | '电话' | 'Email')[]; require_supported_route: boolean; require_party_size: boolean; require_departure_window: boolean; answer_before_asking: boolean; ask_after_answered_topics: number };
  routing: { enabled_route_variants: string[]; allow_route_switch: boolean; preserve_profile_on_switch: boolean; outside_catalog_action: 'recommend_supported_routes' | 'explain_boundary_only' };
  handoff: { large_group_enabled: boolean; large_group_minimum: number };
  business_rules: BusinessRule[];
  silence: { enabled: boolean; intervals_minutes: number[]; v2_intervals_minutes?: number[]; max_proactive_messages_per_day: number; active_start: string; active_end: string };
  stage_journey: { mandatory_send: boolean; skip_when_no_relevant_content: boolean; model_max_attempts: number; model_failure_action: 'warn_and_skip_touch'; stages: string[]; touch_goals: string[] };
};

export type ReceptionVersion = {
  label: string; prompt_version: string; validator_version: string;
  status: 'published'; published_at: string | null; published_by: number | null;
};

export type ReceptionHistory = {
  id: number; label: string; summary: string; user_id: number | null; created_at: string;
};

export type ConfigResponse = {
  config: ReceptionConfig;
  version: ReceptionVersion;
  model_nodes: {
    customer_understanding: string;
    business_planner: string;
    reply_generation: string;
    reply_fact_verification: string;
    silence_planner: string;
    silence_generation: string;
    silence_touch: string;
  };
  history: ReceptionHistory[];
  hard_guards: Record<string, boolean | string>;
  outbound: false;
};

export type ContentDraft = {
  name: string; selection_title: string; default_entry_message: string;
  ai_guidance: string; match_keywords: string[]; knowledge_facts: ProductFact[]; content_groups: ContentGroup[];
  content_sequence: string[]; fixed_answers: FixedAnswer[]; initial_delivery_interval_seconds: number;
};

export const groupLabels: Record<string, string> = {
  advisor_greeting: '线路确认承接语', brand_positioning: '品牌定位', rongbuk_reference: '绒布旅馆',
  itinerary_overview: '行程总览', peach_highlights: '线路亮点', hotel_reference: '住宿',
  vehicle_reference: '车辆', price_reference: '价格', contact_request: '索取联系方式',
  entry_question: '询问人数', party_question: '询问人数', departure_question: '询问时间',
  departure_reference: '出发安排', accommodation_summary: '住宿补充', landmarks: '经典景点',
  spring_weather: '冷暖与穿衣',
  zhaji: '人文景点', read_check: '沉默承接', contact_transition: '留资过渡',
  price_deferral: '价格承接', party_intro_solo: '单人承接', party_intro_small: '小团承接',
  party_intro_group: '多人承接', summit_reference: '珠峰安排', summit_accommodation: '珠峰住宿',
};

export const actionLabels: Record<BusinessRule['action'], string> = {
  handoff: '转人工', request_contact: '索取联系方式', recommend_routes: '推荐现有线路',
  continue_ai: '继续 AI 接待', stop_ai: '停止 AI',
};

export const protectedRules = new Set(['captured_contact', 'explicit_human', 'service_dispute', 'attachment_review']);

export const defaultRules: BusinessRule[] = [
  { id: 'large_group', name: '大团交给人工', enabled: true, condition: '客户明确同行人数达到配置的大团人数阈值', action: 'handoff', guidance: '确认人数后说明由顾问继续制定安排，不承诺价格或余位。', system_key: 'large_group' },
  { id: 'captured_contact', name: '取得联系方式后交给人工', enabled: true, condition: '客户提供了有效的 LINE、微信、电话或 Email', action: 'handoff', guidance: '确认收到联系方式并说明顾问会继续跟进。', system_key: 'captured_contact' },
  { id: 'explicit_human', name: '客户要求真人', enabled: true, condition: '客户明确要求真人客服或顾问接待', action: 'handoff', guidance: '简短确认，停止 AI 继续营销。', system_key: 'explicit_human' },
  { id: 'service_dispute', name: '售后争议交给人工', enabled: true, condition: '客户正在处理投诉、退款或合同争议', action: 'handoff', guidance: '不承诺处理结果，由人工根据订单和条款继续处理。', system_key: 'service_dispute' },
  { id: 'attachment_review', name: '必须查看附件', enabled: true, condition: '回答依赖客户本轮图片或文件的实际内容，当前无法可靠识别', action: 'handoff', guidance: '说明需要顾问查看附件，不追加线路或留资问题。', system_key: 'attachment_review' },
];

export function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T; }

export function normalizeConfig(value: ReceptionConfig): ReceptionConfig {
  const result = clone(value);
  result.reply.tone_guidance ??= '';
  result.reply.opening_message ??= '您好～這裡是 China2Go 國旅環球，您想先了解哪一條行程呢？';
  result.reply.opening_messages ??= [result.reply.opening_message];
  result.reply.opening_items ??= result.reply.opening_messages.map((content, index) => ({
    key: `opening-${index}`, content_type: 'text', content,
  }));
  result.reply.opening_interval_seconds ??= 2;
  return { ...result, schema_version: Math.max(value.schema_version ?? 0, 5), business_rules: clone(value.business_rules?.length ? value.business_rules : defaultRules) };
}

export function productDraft(product: RouteProduct): ContentDraft {
  return {
    name: product.name,
    selection_title: product.selection_title,
    default_entry_message: product.default_entry_message,
    ai_guidance: product.ai_guidance ?? '',
    match_keywords: clone(product.match_keywords ?? []),
    knowledge_facts: clone(product.knowledge_facts),
    content_groups: clone(product.content_groups),
    content_sequence: clone(product.content_sequence),
    fixed_answers: clone(product.fixed_answers ?? []),
    initial_delivery_interval_seconds: product.initial_delivery_interval_seconds ?? 2,
  };
}
