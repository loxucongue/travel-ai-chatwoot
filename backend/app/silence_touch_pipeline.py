"""Split silence-touch pipeline: code planning, wording, then fact verification."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.deepseek_evaluation import EvaluationCallError, EvaluationDecision
from app.model_gateway import combine_digests
from app.reception_policy_views import reception_policy_views
from app.reply_fact_verification import (
    FACT_VERIFIER_PROMPT_VERSION,
    FactVerification,
    call_reply_fact_verifier,
)
from app.reply_generation import (
    GeneratedReply,
    deterministic_system_reply,
)
from app.reply_planning import ReplyPlan
from app.route_packages import ROUTES
from app.silence_generation import (
    SILENCE_GENERATOR_PROMPT_VERSION,
    call_silence_generator,
)
from app.silence_planning import SILENCE_PLANNER_VERSION, SilencePlan, build_silence_plan


SILENCE_TOUCH_PROMPT_VERSION = "split-silence-touch-v13"


def _covered_groups(plan: ReplyPlan, generated: GeneratedReply | None) -> list[str]:
    if not generated or not plan.route_variant:
        return []
    route = ROUTES[plan.route_variant]
    used_facts = set(generated.used_fact_ids)
    used_assets = set(generated.asset_ids)
    covered = [
        key
        for key in plan.allowed_content_group_keys
        if key in route["groups"]
        and (
            used_facts & set(route["groups"][key].get("evidence", []))
            or used_assets & set(route["groups"][key].get("assets", []))
        )
    ]
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


def _decision(
    silence_plan: SilencePlan,
    generated: GeneratedReply | None,
    *,
    legacy_wakeup: bool = False,
) -> EvaluationDecision:
    plan = silence_plan.reply_plan
    covered = _covered_groups(plan, generated)
    return EvaluationDecision(
        action=plan.action,
        branch=plan.branch,
        intent=plan.intent,
        reply=generated.reply if generated else None,
        slots={},
        missing_slots=plan.missing_slots,
        handoff_reason=plan.handoff_reason,
        evidence_refs=generated.used_fact_ids if generated else [],
        safety_flags=sorted(set([
            *plan.safety_flags,
            *(["stop_automation"] if plan.stop_automation else []),
        ])),
        confidence=plan.confidence,
        slot_evidence={},
        material_keys=generated.asset_ids if generated else [],
        wakeup_action=(
            "generate" if plan.action == "reply" else "handoff" if plan.action == "handoff" else "skip"
        ) if legacy_wakeup else None,
        route_variant=plan.route_variant,
        route_evidence="",
        lead_action=plan.lead_action,
        contact_values={},
        content_group_key=covered[0] if covered else "",
        covered_content_groups=covered,
        reply_options=[],
        allow_material_resend=False,
        journey_stage=plan.next_stage,
        touch_goal=silence_plan.touch_goal,
        touch_reason=silence_plan.touch_reason,
        profile_updates={},
        reply_body=generated.body if generated else "",
        follow_up_type=generated.follow_up.type if generated and generated.follow_up else "",
        follow_up_field=generated.follow_up.field if generated and generated.follow_up else "",
        follow_up_question=generated.follow_up.question if generated and generated.follow_up else "",
    )


def run_silence_touch_pipeline(
    context: dict,
    *,
    generation_node: Callable[[dict, SilencePlan], tuple[GeneratedReply, list[dict], str]] = call_silence_generator,
    verification_node: Callable[[dict, ReplyPlan, GeneratedReply], tuple[FactVerification, list[dict], str]] = call_reply_fact_verifier,
) -> tuple[EvaluationDecision, list[dict], str, dict]:
    runtime_context = {
        **context,
        "reception_policy_views": reception_policy_views(context.get("reception_policy") or {}),
    }
    silence_plan = build_silence_plan(runtime_context)
    plan = silence_plan.reply_plan
    generated = deterministic_system_reply(plan)
    generation_logs: list[dict] = []
    verification_logs: list[dict] = []
    digests = [combine_digests(SILENCE_TOUCH_PROMPT_VERSION, repr(silence_plan.to_dict()))]
    generation_rounds = 0
    fact_verification_passed: bool | None = None
    if plan.action != "no_action" and generated is None:
        try:
            generated, current_logs, current_digest = generation_node(runtime_context, silence_plan)
            generation_logs.extend(current_logs)
            digests.append(current_digest)
            generation_rounds = 1
            verification, current_logs, current_digest = verification_node(runtime_context, plan, generated)
            verification_logs.extend(current_logs)
            digests.append(current_digest)
            fact_verification_passed = verification.supported
            if not verification.supported or not verification.relevant:
                rewrite_context = {
                    **runtime_context,
                    "reply_generation_feedback": {
                        "unsupported_claims": verification.unsupported_claims,
                        "unanswered_questions": verification.unanswered_questions,
                        "instruction": "刪除不受支持的說法，只使用 allowed_facts 重寫本次沉默跟進。",
                    },
                }
                generated, current_logs, current_digest = generation_node(rewrite_context, silence_plan)
                generation_logs.extend(current_logs)
                digests.append(current_digest)
                generation_rounds = 2
                verification, current_logs, current_digest = verification_node(rewrite_context, plan, generated)
                verification_logs.extend(current_logs)
                digests.append(current_digest)
                fact_verification_passed = verification.supported
                if not verification.supported or not verification.relevant:
                    plan = replace(
                        plan,
                        action="no_action",
                        next_stage=str((context.get("journey") or {}).get("stage") or "needs_discovery"),
                        allowed_fact_ids=[],
                        allowed_asset_ids=[],
                        allowed_content_group_keys=[],
                        follow_up=None,
                        lead_action="none",
                        safety_flags=list(dict.fromkeys([
                            *plan.safety_flags,
                            "silence_verification_failed_no_action",
                        ])),
                    )
                    silence_plan = replace(
                        silence_plan,
                        reply_plan=plan,
                        skip_reason="fact_verification_failed_no_action",
                        touch_reason="事實核驗未通過，本次不發送。",
                    )
                    generated = None
        except EvaluationCallError as exc:
            if exc.code != "reply_repeats_recent_advisor_message":
                raise EvaluationCallError(
                    exc.code,
                    [*generation_logs, *verification_logs, *exc.logs],
                    combine_digests(*digests, exc.digest),
                ) from exc
            # Preserve rejected attempts; never mark them as delivered or turn
            # network/model failures into a successful skip.
            generation_logs.extend(exc.logs)
            digests.append(exc.digest)
            plan = replace(plan, action="no_action", follow_up=None,
                           allowed_fact_ids=[], allowed_asset_ids=[], allowed_content_group_keys=[],
                           lead_action="none", next_stage=str((context.get("journey") or {}).get("stage") or "needs_discovery"),
                           safety_flags=[*plan.safety_flags, "silence_duplicate_skipped"])
            silence_plan = replace(silence_plan, reply_plan=plan,
                                   skip_reason="silence_duplicate_skipped",
                                   touch_reason="本輪內容已說明，沒有新的資訊可補充。")
            generated = None
    elif generated is not None:
        fact_verification_passed = None

    decision = _decision(
        silence_plan,
        generated,
        legacy_wakeup=str(context.get("module") or "") == "wakeup",
    )
    trace = {
        "pipeline": "split_silence_touch",
        "prompt_version": SILENCE_TOUCH_PROMPT_VERSION,
        "planner_version": SILENCE_PLANNER_VERSION,
        "silence_generator_prompt_version": SILENCE_GENERATOR_PROMPT_VERSION,
        "fact_verifier_prompt_version": FACT_VERIFIER_PROMPT_VERSION,
        "planned_action": plan.action,
        "planned_stage": plan.next_stage,
        "planned_follow_up": plan.follow_up.type if plan.follow_up else None,
        "touch_goal": silence_plan.touch_goal,
        "skip_reason": silence_plan.skip_reason,
        "response_source": (
            "deterministic_system_copy"
            if generated and generation_rounds == 0
            else "model_rewrite"
            if generation_rounds > 1
            else "model"
            if generated
            else "none"
        ),
        "generation_rounds": generation_rounds,
        "fact_verification_passed": fact_verification_passed,
        "fact_verification_mode": "model_check" if verification_logs else "system_copy" if generated else "not_run",
        "allowed_fact_count": len(plan.allowed_fact_ids),
        "allowed_asset_count": len(plan.allowed_asset_ids),
    }
    return decision, [*generation_logs, *verification_logs], combine_digests(*digests), trace
