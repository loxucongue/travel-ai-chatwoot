"""Split real-time reply pipeline: understand, plan in code, then verbalize."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.deepseek_evaluation import EvaluationCallError, EvaluationDecision
from app.model_gateway import combine_digests
from app.reception_policy_views import reception_policy_views
from app.reply_generation import (
    REPLY_GENERATOR_PROMPT_VERSION,
    GeneratedReply,
    call_reply_generator,
    deterministic_system_reply,
)
from app.reply_fact_verification import (
    FACT_VERIFIER_PROMPT_VERSION,
    FactVerification,
    call_reply_fact_verifier,
)
from app.reply_planning import PLANNER_VERSION, ReplyPlan, build_reply_plan
from app.reply_understanding import (
    UNDERSTANDING_PROMPT_VERSION,
    CustomerUnderstanding,
    call_customer_understanding,
)
from app.route_packages import ROUTES
from app.web_knowledge import select_understood_web_facts


REALTIME_REPLY_PROMPT_VERSION = "split-realtime-reply-v45"


def _covered_groups(plan: ReplyPlan, generated: GeneratedReply | None) -> list[str]:
    if not generated or not plan.route_variant:
        return []
    route = ROUTES[plan.route_variant]
    used_facts = set(generated.used_fact_ids)
    used_assets = set(generated.asset_ids)
    covered = []
    for key in plan.allowed_content_group_keys:
        if key not in route["groups"]:
            continue
        group = route["groups"][key]
        used_group_facts = used_facts & set(group.get("evidence", []))
        used_group_assets = used_assets & set(group.get("assets", []))
        # This compatibility field identifies touched groups. The execution
        # ledger decides completion from the text and each confirmed asset.
        if used_group_facts or used_group_assets:
            covered.append(key)
    if not covered and len(plan.allowed_content_group_keys) == 1 and generated.body.strip():
        only = plan.allowed_content_group_keys[0]
        covered.append(only)
    if plan.follow_up:
        policies = route.get("policies", {})
        follow_group = None
        if plan.follow_up.type == "contact":
            follow_group = policies.get("contact_request_group")
        elif plan.follow_up.field == "party_size":
            follow_group = policies.get("party_question_group")
        elif plan.follow_up.field == "departure_window":
            follow_group = policies.get("departure_question_group")
        if follow_group in route["groups"]:
            covered.append(follow_group)
    return list(dict.fromkeys(covered))


def _compatibility_decision(plan: ReplyPlan, generated: GeneratedReply | None) -> EvaluationDecision:
    covered = _covered_groups(plan, generated)
    return EvaluationDecision(
        action=plan.action,
        branch=plan.branch,
        intent=plan.intent,
        reply=generated.reply if generated else None,
        slots=plan.slots,
        missing_slots=plan.missing_slots,
        handoff_reason=plan.handoff_reason,
        evidence_refs=generated.used_fact_ids if generated else [],
        safety_flags=sorted(set([
            *plan.safety_flags,
            *(["stop_automation"] if plan.stop_automation else []),
        ])),
        confidence=plan.confidence,
        slot_evidence=plan.slot_evidence,
        material_keys=generated.asset_ids if generated else [],
        wakeup_action=None,
        defer_minutes=0,
        route_variant=plan.route_variant,
        route_evidence=plan.route_evidence,
        lead_action=plan.lead_action,
        contact_values=plan.contact_values,
        content_group_key=covered[0] if covered else "",
        covered_content_groups=covered,
        reply_options=plan.reply_options if plan.action == "reply" else [],
        allow_material_resend=False,
        journey_stage=plan.next_stage,
        touch_goal="",
        touch_reason="",
        profile_updates={},
        reply_body=generated.body if generated else "",
        opening_messages=plan.opening_messages if generated else [],
        opening_items=plan.opening_items if generated else [],
        opening_interval_seconds=plan.opening_interval_seconds,
        follow_up_type=generated.follow_up.type if generated and generated.follow_up else "",
        follow_up_field=generated.follow_up.field if generated and generated.follow_up else "",
        follow_up_question=generated.follow_up.question if generated and generated.follow_up else "",
    )


def run_realtime_reply_pipeline(
    context: dict,
    *,
    understanding_node: Callable[[dict], tuple[CustomerUnderstanding, list[dict], str]] = call_customer_understanding,
    generation_node: Callable[[dict, ReplyPlan], tuple[GeneratedReply, list[dict], str]] = call_reply_generator,
    verification_node: Callable[[dict, ReplyPlan, GeneratedReply], tuple[FactVerification, list[dict], str]] = call_reply_fact_verifier,
) -> tuple[EvaluationDecision, list[dict], str, dict]:
    runtime_context = {
        **context,
        "reception_policy_views": reception_policy_views(context.get("reception_policy") or {}),
    }
    understanding, understanding_logs, understanding_digest = understanding_node(runtime_context)
    runtime_context = select_understood_web_facts(runtime_context, understanding)
    from app.decision_knowledge import evidence_packet
    runtime_context["knowledge"] = evidence_packet(runtime_context.get("global_knowledge_facts"),
                                                  runtime_context.get("global_knowledge_version", ""))
    plan = build_reply_plan(runtime_context, understanding)
    generated: GeneratedReply | None = deterministic_system_reply(plan)
    generation_logs: list[dict] = []
    verification_logs: list[dict] = []
    digests = [understanding_digest]
    generation_rounds = 0
    generation_source = ""
    fact_verification_passed: bool | None = None
    relevance_passed: bool | None = None
    verification = None
    mandatory_confirmation = bool(plan.action == "reply" and set(plan.safety_flags) & {
        "availability_confirmation_required", "current_conditions_confirmation_required", "customization_planning_required"})
    confirmation_notice = "待核對的部分，我這邊安排專項顧問接著為您確認。"
    if mandatory_confirmation:
        # Keep answerable facts in the normal generation/verification chain. The
        # adapter still creates the handoff task before any terminal message.
        plan = replace(plan, follow_up=None, lead_action="none", allowed_asset_ids=[],
                       allowed_content_group_keys=[], fixed_answer_id="", fixed_answer_text="")
        runtime_context = {**runtime_context, "reply_reserved_characters": len(confirmation_notice) + 2}
        generated = None
    if plan.action != "no_action" and generated is None:
        try:
            generated, initial_logs, initial_digest = generation_node(runtime_context, plan)
            generation_source = initial_digest
            generation_logs.extend(initial_logs)
            digests.append(initial_digest)
            if plan.fixed_answer_id:
                # Active fixed answers are reviewed, immutable content assets.
                # Re-running them through a model would weaken the verbatim
                # contract and can only add latency or reject approved copy.
                generation_rounds = 0
                fact_verification_passed = None
            else:
                generation_rounds = 1
                verification, current_logs, current_digest = verification_node(runtime_context, plan, generated)
                verification_logs.extend(current_logs)
                digests.append(current_digest)
                fact_verification_passed = verification.supported
                relevance_passed = verification.relevant
            if not plan.fixed_answer_id and (not fact_verification_passed or not relevance_passed):
                if plan.action == "reply" and verification.unsupported_claims:
                    # Unsupported product associations must not force a photo
                    # or advance the journey while the answer is being corrected.
                    plan = replace(
                        plan, allowed_asset_ids=[], allowed_content_group_keys=[],
                        follow_up=None, lead_action="none",
                        next_stage="needs_discovery" if plan.next_stage == "contact_requested" else plan.next_stage,
                        safety_flags=list(dict.fromkeys([*plan.safety_flags, "fact_rewrite_text_only"])),
                    )
                rewrite_context = {
                    **runtime_context,
                    "reply_generation_feedback": {
                        "unsupported_claims": verification.unsupported_claims,
                        "unanswered_questions": verification.unanswered_questions,
                        "instruction": "只使用 allowed_facts 重寫正文；刪除無依據說法，並直接回應遺漏問題。無依據時明確說明需核對，不得改談主線或人數。",
                    },
                }
                generated, rewrite_logs, rewrite_digest = generation_node(rewrite_context, plan)
                generation_source = rewrite_digest
                generation_logs.extend(rewrite_logs)
                digests.append(rewrite_digest)
                generation_rounds = 2
                verification, rewrite_check_logs, rewrite_check_digest = verification_node(
                    rewrite_context, plan, generated
                )
                verification_logs.extend(rewrite_check_logs)
                digests.append(rewrite_check_digest)
                fact_verification_passed = verification.supported
                relevance_passed = verification.relevant
                if not fact_verification_passed or not relevance_passed:
                    raise EvaluationCallError(
                        "reply_verification_failed", [], combine_digests(*digests)
                    )
        except EvaluationCallError as exc:
            generation_logs.extend(exc.logs)
            digests.append(exc.digest)
            if exc.code == "reply_verification_failed" or (exc.code.startswith("reply_too_long") and generation_rounds >= 2):
                # Existing adapters persist the handoff task before submitting this notice.
                plan = replace(plan, action="handoff", handoff_reason="knowledge_verification_required",
                               next_stage="handoff", follow_up=None, lead_action="none",
                               allowed_asset_ids=[], allowed_fact_ids=[], allowed_content_group_keys=[],
                               safety_flags=list(dict.fromkeys([*plan.safety_flags, "verification_requires_handoff"])))
                generated = GeneratedReply("這個問題需要再核對一下，稍等一下，我這邊安排專項顧問接著為您確認。", None, [], [])
                generation_source = "verification_handoff_notice"
            elif (exc.code.startswith("reply_too_long") or (exc.code in {"selected_assets_not_described", "reply_duplicate_asset_narration"} and plan.allowed_asset_ids)) and generation_rounds < 2:
                plan = replace(
                    plan,
                    allowed_asset_ids=[],
                    allowed_content_group_keys=[], follow_up=None, lead_action="none",
                    safety_flags=list(dict.fromkeys([
                        *plan.safety_flags,
                        "asset_caption_failed_text_only",
                    ])),
                )
                fallback_context = {
                    **runtime_context,
                    "reply_generation_feedback": {
                        "instruction": (
                            "本輪取消圖片。直接回答客戶問題，不得聲稱已傳送圖片；"
                            "只使用 allowed_facts，不增加追問。每個子問題保留一個直接答案，"
                            "詢問費用加景點時保留價格與計價條件、核心景點；不要抄完所有費用清單或沿途城市。"
                        ),
                    },
                }
                try:
                    generated, fallback_logs, fallback_digest = generation_node(fallback_context, plan)
                    generation_logs.extend(fallback_logs)
                    digests.append(fallback_digest)
                    generation_source = fallback_digest
                    generation_rounds = 2
                    verification, fallback_check_logs, fallback_check_digest = verification_node(
                        fallback_context, plan, generated
                    )
                    verification_logs.extend(fallback_check_logs)
                    digests.append(fallback_check_digest)
                    fact_verification_passed = verification.supported
                    relevance_passed = verification.relevant
                    if fact_verification_passed and relevance_passed:
                        exc = None
                except EvaluationCallError as fallback_exc:
                    exc = EvaluationCallError(
                        fallback_exc.code,
                        [*exc.logs, *fallback_exc.logs],
                        combine_digests(exc.digest, fallback_exc.digest),
                    )
                if exc is None:
                    pass
                else:
                    plan = replace(plan, action="handoff", handoff_reason="knowledge_verification_required",
                                   next_stage="handoff", follow_up=None, lead_action="none",
                                   allowed_asset_ids=[], allowed_fact_ids=[], allowed_content_group_keys=[],
                                   safety_flags=list(dict.fromkeys([*plan.safety_flags, "verification_requires_handoff"])))
                    generated = GeneratedReply("這個問題需要再核對一下，稍等一下，我這邊安排專項顧問接著為您確認。", None, [], [])
                    generation_source = "verification_handoff_notice"
            else:
                raise EvaluationCallError(
                    exc.code,
                    [*understanding_logs, *generation_logs, *verification_logs, *exc.logs],
                    combine_digests(*digests, exc.digest),
                ) from exc
    elif generated is not None:
        fact_verification_passed = None
    if (plan.action == "reply" and verification is not None
            and (verification.confirmation_questions or mandatory_confirmation)):
        limit = runtime_context["reception_policy_views"]["runtime_policy"]["reply_limits"]["max_characters"]
        needs_terminal_rewrite = bool(generated.asset_ids or len(generated.body) + len(confirmation_notice) + 2 > limit)
        if needs_terminal_rewrite and generation_rounds < 2:
            pending_questions = list(verification.confirmation_questions)
            plan = replace(plan, follow_up=None, lead_action="none", allowed_asset_ids=[], allowed_content_group_keys=[])
            rewrite_context = {**runtime_context,
                "reply_reserved_characters": len(confirmation_notice) + 2,
                "reply_generation_feedback": {"instruction": (
                    "改成純文字，保留有依據的直接答案與具體待核對項目。"
                    "不得聲稱傳送圖片，不追加問題或轉交說明，程式會在最後附上核對說明。"
                )}}
            try:
                rewritten, rewrite_logs, rewrite_digest = generation_node(rewrite_context, plan)
                generation_logs.extend(rewrite_logs)
                digests.append(rewrite_digest)
                generation_rounds = 2
                checked, check_logs, check_digest = verification_node(rewrite_context, plan, rewritten)
                verification_logs.extend(check_logs)
                digests.append(check_digest)
                if checked.supported and checked.relevant:
                    generated = rewritten
                    verification = replace(checked, confirmation_questions=list(dict.fromkeys([
                        *pending_questions, *checked.confirmation_questions,
                    ])))
            except EvaluationCallError as exc:
                generation_logs.extend(exc.logs)
                digests.append(exc.digest)
        if mandatory_confirmation and not verification.confirmation_questions:
            verification = replace(verification, confirmation_questions=[str(context.get("customer_text") or "")])
        plan = replace(plan, action="handoff", handoff_reason="knowledge_confirmation_required",
                       next_stage="handoff", follow_up=None, lead_action="none",
                       allowed_asset_ids=[], allowed_content_group_keys=[],
                       safety_flags=list(dict.fromkeys([*plan.safety_flags, "knowledge_requires_handoff"])))
        notice = confirmation_notice
        body = generated.body + "\n\n" + notice
        limit = runtime_context["reception_policy_views"]["runtime_policy"]["reply_limits"]["max_characters"]
        if len(body) <= limit and not generated.asset_ids:
            generated = GeneratedReply(body, None, generated.used_fact_ids, [])
        else:
            generated = GeneratedReply("您問的這部分需要再核對一下，稍等一下，我這邊安排專項顧問接著為您確認。", None, [], [])
        generation_source = "knowledge_confirmation_notice"
    if (generated is not None and plan.action == "reply" and not plan.fixed_answer_id
            and plan.allowed_content_group_keys and plan.allowed_content_group_keys != ["advisor_greeting"]
            and not generated.used_fact_ids and not generated.asset_ids):
        plan = replace(plan, allowed_content_group_keys=[], safety_flags=list(dict.fromkeys(
            [*plan.safety_flags, "skip_silence_enrollment"]
        )))
    decision = _compatibility_decision(plan, generated)
    if generated and plan.action == "reply" and not plan.fixed_answer_id:
        limit = runtime_context["reception_policy_views"]["runtime_policy"]["reply_limits"]["max_messages_per_turn"]
        if limit == 1 and generated.follow_up:
            decision.reply_body = generated.reply
            decision.follow_up_question = decision.follow_up_field = decision.follow_up_type = ""
        capacity = max(1, min(3, limit - len(decision.material_keys) - int(bool(decision.follow_up_question))))
        paragraphs = [part.strip() for part in decision.reply_body.split("\n\n") if part.strip()]
        if len(paragraphs) > capacity:
            paragraphs = [*paragraphs[:capacity - 1], "\n\n".join(paragraphs[capacity - 1:])]
        decision.reply_segments = paragraphs
        decision.reply_body = "\n\n".join(paragraphs)
        decision.reply = (decision.reply_body + (" " + decision.follow_up_question if decision.follow_up_question else "")).strip()
    digest = combine_digests(*digests)
    trace = {
        "pipeline": "split_realtime_reply",
        "prompt_version": REALTIME_REPLY_PROMPT_VERSION,
        "understanding_prompt_version": UNDERSTANDING_PROMPT_VERSION,
        "planner_version": PLANNER_VERSION,
        "reply_generator_prompt_version": REPLY_GENERATOR_PROMPT_VERSION,
        "fact_verifier_prompt_version": FACT_VERIFIER_PROMPT_VERSION,
        "route_resolution": understanding.route_resolution,
        "semantic_signals": understanding.semantic_signals,
        "customer_questions": understanding.customer_questions,
        "question_details": understanding.question_details,
        "discussion_subject": understanding.discussion_subject,
        "historical_route_choice": understanding.historical_route_choice,
        "confirmation_questions": verification.confirmation_questions if verification else [],
        "unsupported_claims": verification.unsupported_claims if verification else [],
        "unanswered_questions": verification.unanswered_questions if verification else [],
        "knowledge_selection": runtime_context.get("knowledge_selection"),
        "selected_web_facts": runtime_context.get("global_knowledge_facts", []),
        "matched_rule_ids": understanding.matched_rule_ids,
        "planned_action": plan.action,
        "planned_stage": plan.next_stage,
        "planned_follow_up": plan.follow_up.type if plan.follow_up else None,
        "response_source": (
            f"{plan.fixed_answer_scope or 'route'}_fixed_answer"
            if plan.fixed_answer_id
            else
            "deterministic_system_copy"
            if generated and generation_rounds == 0
            else "model_rewrite"
            if generation_rounds > 1
            else "model"
            if generated
            else "none"
        ),
        "generation_rounds": generation_rounds,
        "generation_source": generation_source,
        "fixed_answer_id": plan.fixed_answer_id or None,
        "fixed_answer_source_ref": plan.fixed_answer_source_ref or None,
        "fixed_answer_scope": plan.fixed_answer_scope or None,
        "fact_verification_passed": fact_verification_passed,
        "fact_verification_mode": (
            "model_check" if verification_logs or generation_rounds else
            "reviewed_copy" if plan.fixed_answer_id else "system_copy" if generated else "not_run"
        ),
        "question_coverage_passed": relevance_passed,
        "allowed_fact_count": len(plan.allowed_fact_ids),
        "allowed_fact_ids": list(plan.allowed_fact_ids),
        "reviewed_web_fact_count": sum(
            1 for fact_id in plan.allowed_fact_ids if str(fact_id).startswith("web.")
        ),
        "allowed_asset_count": len(plan.allowed_asset_ids),
    }
    return decision, [*understanding_logs, *generation_logs, *verification_logs], digest, trace
