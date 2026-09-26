import { Badge } from './components';
import { deliveryItem, deliveryState, verificationBlocked, type RuntimeEvidence } from './delivery';
import './delivery.css';

export function DeliveryStatus({ status }: { status?: unknown }) {
  const value = deliveryState(status);
  return <Badge tone={value.tone}>{value.label}</Badge>;
}

export function DeliveryDetails({ status, attributes, record }: { status?: unknown; attributes?: unknown; record?: unknown }) {
  const item = deliveryItem(attributes) ?? deliveryItem(record);
  return <span className="delivery-details">
    <span>{'消息回执：'}<DeliveryStatus status={status} /></span>
    {item ? <span>{'投递项：'}<DeliveryStatus status={item.status} /></span> : null}
    {item ? ['plan_version', 'group_key', 'item_id'].map(key => {
      const value = item[key];
      return typeof value === 'string' || typeof value === 'number'
        ? <span key={key}>{key}: <b>{value}</b></span> : null;
    }) : null}
  </span>;
}

export function RuntimeDetails({ run }: { run: RuntimeEvidence }) {
  const blocked = verificationBlocked(run);
  return <div className="delivery-details">
    {blocked ? <DeliveryStatus status="verification_blocked" /> : run.decision.action === 'no_action' ? <DeliveryStatus status="no_action" /> : null}
    <span>{'事实校验：'}{run.trace.fact_verification_passed === true ? '通过' : run.trace.fact_verification_passed === false ? '未通过' : run.trace.fact_verification_mode === 'reviewed_copy' ? '已审核原文，未调用模型核验' : run.trace.fact_verification_mode === 'system_copy' ? '固定系统文案，未调用模型核验' : '未执行'}</span>
    <span>{'问题覆盖校验：'}{run.trace.question_coverage_passed === true ? '通过' : run.trace.question_coverage_passed === false ? '未通过' : '未提供'}</span>
    {run.trace.skip_reason ? <span>{'跳过原因：'}{run.trace.skip_reason}</span> : null}
    {run.decision.safety_flags?.includes('customer_paused_proactive') ? <span>客户暂缓：停止主动触达，等待客户重新咨询</span> : null}
    {run.decision.safety_flags?.includes('verification_requires_handoff') ? <span>原回答未通过核验，已改为人工核对流程</span> : null}
    {run.trace.discussion_subject ? <span>当前讨论：{run.trace.discussion_subject}</span> : null}
    {run.trace.confirmation_questions?.map((question, index) => <span key={`confirmation:${index}`}>待核对：{question}</span>)}
    {run.trace.handoff_task ? <span>模拟跟进任务：{run.trace.handoff_task.id} · {run.trace.handoff_task.status === 'pending' ? '待处理' : run.trace.handoff_task.status}</span> : null}
    {run.trace.question_details?.map((item, index) => <span key={index}>问题 {index + 1}：{item.question}{item.reference_message_id ? `（承接消息 ${item.reference_message_id}）` : ''}</span>)}
    {run.trace.knowledge_selection ? <details><summary>资料选择记录</summary>
      <p>选用候选：{run.trace.knowledge_selection.selected_fact_ids?.join('、') || '没有匹配资料'}</p>
      {run.trace.knowledge_selection.rejected?.map(item => <p key={item.fact_id}>{item.fact_id}：{item.reason === 'question_topic_mismatch' ? '与本轮问题主题不符' : item.reason}</p>)}
    </details> : null}
  </div>;
}
