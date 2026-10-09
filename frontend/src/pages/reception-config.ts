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
  assets: ProductAsset[];
  versions: ProductVersion[];
  readiness: {
    assets_ready: number; assets_total: number; missing_assets: string[];
    ai_reply_ready: boolean; sop_ready: boolean;
  };
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
  intake: { enabled: boolean; question: string; options: string[]; wait_seconds: number };
  common_scripts: {id: string; name: string; scenario: string; text: string; enabled: boolean}[];
  reply: { opening_message: string; opening_messages: string[]; opening_items: OpeningItem[]; opening_interval_seconds: number; goal: string; tone: 'friendly_professional' | 'concise' | 'warm'; tone_guidance: string; opening_character_limit: number; custom_guidance: string };
  lead_capture: { enabled: boolean; channels: ('LINE' | '微信' | '电话' | 'Email' | 'WhatsApp')[] };
  routing: { enabled_route_variants: string[]; allow_route_switch: boolean; preserve_profile_on_switch: boolean; outside_catalog_action: 'recommend_supported_routes' | 'explain_boundary_only' | 'consult_advisor' };
  handoff: { large_group_enabled: boolean; large_group_minimum: number };
  silence: { live_enabled?: boolean | null; enabled: boolean; intervals_minutes?: number[]; max_proactive_messages_per_day: number; active_start: string; active_end: string };
};

export type ReceptionVersion = {
  label: string;
  status: 'published'; published_at: string | null; published_by: number | null;
};

export type ReceptionHistory = {
  id: number; label: string; summary: string; user_id: number | null; created_at: string;
};

export type ConfigResponse = {
  config: ReceptionConfig;
  version: ReceptionVersion;
  runtime: {live_silence_enabled: boolean; model: string; timeout_seconds: number; concurrency: number};
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
  spring_weather: '冷暖与穿衣', vehicle_oxygen: '移动供氧配置', no_shopping: '零购物承诺',
  zhaji: '人文景点', read_check: '沉默承接', contact_transition: '留资过渡',
  price_deferral: '价格承接', party_intro_solo: '单人承接', party_intro_small: '小团承接',
  party_intro_group: '多人承接', summit_reference: '珠峰安排', summit_accommodation: '珠峰住宿',
};

export function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T; }

export function normalizeConfig(value: ReceptionConfig): ReceptionConfig {
  const result = clone(value);
  result.common_scripts ??= [];
  result.intake ??= {enabled: true, question: '您好，請問預計幾位想來旅遊？\n（幫我回答一下大概人數，才能快速幫助您匹配適合的方案跟團型）', options: ['自己一位','2～3位','4～6位','7～10位','10位以上'], wait_seconds: 60};
  result.silence.intervals_minutes ??= [360];
  result.reply.tone_guidance ??= '';
  result.reply.opening_message ??= '您好～這裡是 China2Go 國旅環球，您想先了解哪一條行程呢？';
  result.reply.opening_messages ??= [result.reply.opening_message];
  result.reply.opening_items ??= result.reply.opening_messages.map((content, index) => ({
    key: `opening-${index}`, content_type: 'text', content,
  }));
  result.reply.opening_interval_seconds ??= 2;
  return { ...result, schema_version: Math.max(value.schema_version ?? 0, 6) };
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

export function configurationChanges(before: ReceptionConfig, after: ReceptionConfig): Record<string, unknown> {
  const fields: Record<string,string[]> = {
    intake: ['enabled','question','options','wait_seconds'],
    reply: ['opening_items','opening_interval_seconds','goal','tone','tone_guidance','custom_guidance'],
    lead_capture: ['enabled','channels'], routing: ['outside_catalog_action'],
    handoff: ['large_group_enabled','large_group_minimum'],
    silence: ['enabled','live_enabled','intervals_minutes','max_proactive_messages_per_day','active_start','active_end'],
  };
  const result: Record<string,unknown> = {};
  for (const [section, keys] of Object.entries(fields)) {
    const prev = before[section as keyof ReceptionConfig] as Record<string,unknown>;
    const next = after[section as keyof ReceptionConfig] as Record<string,unknown>;
    const changes = Object.fromEntries(keys.filter(key => JSON.stringify(prev[key]) !== JSON.stringify(next[key])).map(key => [key,next[key]]));
    if (Object.keys(changes).length) result[section]=changes;
  }
  if (JSON.stringify(before.common_scripts)!==JSON.stringify(after.common_scripts)) result.common_scripts=after.common_scripts;
  return result;
}

export const followupCheckpoints = (intervals: number[]) => intervals.map((_, index) => intervals.slice(0,index+1).reduce((sum,value) => sum+value,0));
export const followupIntervals = (points: number[]) => points.map((point,index) => point-(points[index-1] ?? 0));
