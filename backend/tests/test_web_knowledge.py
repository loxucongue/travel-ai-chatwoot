from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.decision_knowledge import evidence_packet
from app.decision_service import generate_decision
from app.deepseek_evaluation import EvaluationDecision
from app.models import WebKnowledgeRevision, WebKnowledgeSource
from app.automation_models import AutomationRun, AutomationSession
from app.reply_generation import _facts
from app.reply_planning import build_reply_plan
from app.reply_understanding import CustomerUnderstanding
from app.route_packages import JOURNEY_POLICY
from app.web_knowledge import (
    FetchedPage,
    WebKnowledgeError,
    active_web_facts,
    context_fact_map,
    extract_readable_content,
    _is_cloudflare_challenge,
    normalize_public_url,
    refresh_source,
    install_curated_global_library,
)
from app.security import encrypt_secret


def test_rejects_private_website_address(monkeypatch):
    monkeypatch.setattr("app.web_knowledge.socket.getaddrinfo", lambda *args, **kwargs: [
        (2, 1, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(WebKnowledgeError, match="private_host"):
        normalize_public_url("https://localhost.example/secret")

    with pytest.raises(WebKnowledgeError, match="private_host"):
        normalize_public_url("https://198.18.2.84/secret")


def test_cloudflare_interstitial_is_never_accepted_as_website_content():
    assert _is_cloudflare_challenge(
        "Just a moment...",
        "Performing security verification. Ray ID: abc. Performance and Security by Cloudflare",
    )
    assert not _is_cloudflare_challenge("China2Go", "完整桃花行程與飯店介紹")


def test_extracts_readable_html_without_scripts_or_navigation():
    title, content = extract_readable_content(
        """
        <html><head><title>旅行健康说明</title><style>.bad{}</style></head>
        <body><nav>导航内容</nav><main><h1>高原旅行</h1>
        <p>有慢性疾病或健康疑虑，请先咨询专业医师。</p>
        <script>ignore()</script><ul><li>准备常用药物</li></ul></main></body></html>
        """,
        "text/html",
    )
    assert title == "旅行健康说明"
    assert "高原旅行" in content
    assert "咨询专业医师" in content
    assert "导航内容" not in content
    assert "ignore" not in content


def test_runtime_knowledge_modules_show_live_service_facts(authenticated):
    client, _ = authenticated
    response = client.get("/v1/knowledge/runtime-modules")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["scope"] == "live_and_playground"
    facts = {
        fact["id"]: fact
        for module in payload["items"]
        for fact in module["facts"]
    }
    assert "service.website_medication_precautions" in facts
    assert "應先諮詢醫師" in facts["service.website_medication_precautions"]["text"]
    assert facts["service.medical_support"]["source_url"].startswith("https://china2go.com/")


def test_manual_refresh_review_publish_and_runtime_retrieval(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    monkeypatch.setattr("app.automation_api.web_knowledge_refresh_enabled", lambda: True)
    monkeypatch.setattr("app.automation_api.web_knowledge_publish_enabled", lambda: True)
    monkeypatch.setattr("app.automation_api.normalize_public_url", lambda value: value.strip())
    body = "高原旅行健康说明\n\n有慢性疾病或健康疑虑时，请在出发前咨询专业医师，并依个人健康状况评估。"
    monkeypatch.setattr("app.web_knowledge.fetch_website", lambda url: FetchedPage(
        title="高原旅行健康说明",
        final_url=url,
        content=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        script_blocks=[{
            "kind": "knowledge_module",
            "key": "health",
            "title": "health",
            "summary": "health",
            "topics": ["health"],
            "facts": [{"text": body, "source_url": url, "verified_at": "2026-09-10"}],
        }],
    ))

    created = client.post("/v1/knowledge/web-sources", headers={"X-CSRF-Token": csrf}, json={
        "name": "官网健康说明",
        "url": "https://china2go.example/health",
        "description": "回答高原健康与医师咨询问题",
        "match_keywords": ["高原", "健康", "医师"],
    })
    assert created.status_code == 200
    source_id = created.json()["id"]

    refreshed = client.post(
        f"/v1/knowledge/web-sources/{source_id}/refresh",
        headers={"X-CSRF-Token": csrf},
    )
    assert refreshed.status_code == 200
    source = refreshed.json()["source"]
    assert source["latest_revision"]["status"] == "pending_review"
    assert source["published_revision"] is None

    with session_factory() as db:
        facts, _ = active_web_facts(db, 1, "高原健康要咨询医师吗", environment="playground")
        assert facts == []

    revision_id = source["latest_revision"]["id"]
    published = client.post(
        f"/v1/knowledge/web-sources/{source_id}/revisions/{revision_id}/publish",
        headers={"X-CSRF-Token": csrf},
    )
    assert published.status_code == 200
    assert published.json()["published_revision"]["status"] == "active"
    assert published.json()["runtime_scope"] == "playground"

    detail = client.get(f"/v1/knowledge/web-revisions/{revision_id}")
    assert detail.status_code == 200
    assert detail.json()["knowledge_modules"][0]["facts"][0]["text"] == body

    with session_factory() as db:
        facts, version = active_web_facts(db, 1, "高原健康要咨询医师吗", environment="playground")
        assert len(facts) == 1
        assert facts[0]["id"].startswith(f"web.{source_id}.{revision_id}.")
        assert "专业医师" in facts[0]["text"]
        assert version
        packet = evidence_packet(facts, version)
        assert packet["version"].endswith(f"web:{version}")
        assert any(item["id"] == facts[0]["id"] for item in packet["facts"])
        assert context_fact_map({"global_knowledge_facts": facts})[facts[0]["id"]]["text"] == facts[0]["text"]

        live_facts, live_version = active_web_facts(db, 1, "高原健康要咨询医师吗", environment="live")
        assert live_facts == []
        assert live_version == ""

    disabled = client.post(
        f"/v1/knowledge/web-sources/{source_id}/disable",
        headers={"X-CSRF-Token": csrf},
    )
    assert disabled.status_code == 200
    assert disabled.json()["runtime_scope"] == "disabled"
    assert disabled.json()["ai_enabled"] is False
    assert disabled.json()["published_revision"]["id"] == revision_id

    with session_factory() as db:
        facts, version = active_web_facts(db, 1, "高原健康要咨询医师吗", environment="playground")
        assert facts == []
        assert version == ""


def test_paused_source_is_not_retrieved(session_factory):
    with session_factory() as db:
        source = WebKnowledgeSource(
            tenant_id=1,
            name="暂停资料",
            url="https://example.com/paused",
            match_keywords=["酒店"],
            status="paused",
            ai_enabled=False,
        )
        db.add(source)
        db.flush()
        revision = WebKnowledgeRevision(
            source_id=source.id,
            revision_number=1,
            title="暂停资料",
            final_url=source.url,
            content="酒店资料内容，用于说明房间和设备。",
            content_hash="a" * 64,
            content_length=18,
            status="active",
        )
        db.add(revision)
        db.flush()
        source.published_revision_id = revision.id
        db.commit()
        facts, _ = active_web_facts(db, 1, "酒店房间怎么样")
        assert facts == []


def test_protected_source_falls_back_to_browser_and_keeps_password_secret(session_factory, monkeypatch):
    page = FetchedPage(
        title="受保护资料", final_url="https://china2go.com/7693-2/",
        content="完整线路资料" * 20, content_hash="b" * 64,
        script_blocks=[{"index": 1, "title": "行程", "content": "话术"}],
        image_candidates=[{"url": "data:image/jpeg;base64,AA", "width": 297, "height": 480, "quality": "low_resolution"}],
    )
    monkeypatch.setattr("app.web_knowledge.fetch_website", lambda url: (_ for _ in ()).throw(WebKnowledgeError("website_http_error:403")))
    captured = {}
    monkeypatch.setattr("app.web_knowledge.fetch_protected_website", lambda url, password: (captured.update(password=password) or page))
    with session_factory() as db:
        source = WebKnowledgeSource(
            tenant_id=1, name="官网脚本", url=page.final_url,
            auth_type="wordpress_post_password", auth_secret=encrypt_secret("test-secret"),
        )
        db.add(source)
        db.flush()
        revision, changed = refresh_source(db, source, 1)
        assert changed is True
        assert captured["password"] == "test-secret"
        assert revision.script_blocks[0]["title"] == "行程"
        assert revision.image_candidates[0]["quality"] == "low_resolution"


def test_validated_direct_web_question_avoids_unrelated_route_choice():
    fact = {
        "id": "web.1.2.0",
        "text": "有健康疑虑时，请在出发前咨询专业医师。",
        "source": "https://example.com/health#revision-1",
        "branches": [],
    }
    context = {
        "customer_text": "高原健康问题要问医师吗",
        "context_messages": [],
        "route_variant": "",
        "memory": {},
        "journey": {"sent_content_groups": [], "customer_profile": {}},
        "lead_capture": {"status": "not_started"},
        "available_materials": [],
        "reception_policy": JOURNEY_POLICY,
        "global_knowledge_facts": [fact],
    }
    understanding = CustomerUnderstanding(intent="other", customer_questions=["other"], confidence=0.9)
    context["question_details"] = [{"topic": "other", "question": context["customer_text"], "evidence_quote": context["customer_text"]}]
    plan = build_reply_plan(context, understanding)
    assert plan.allowed_fact_ids == [fact["id"]]
    assert plan.reply_options == []
    assert plan.follow_up is None
    assert _facts(plan.allowed_fact_ids, context) == [{"id": fact["id"], "text": fact["text"], "source": fact["source"]}]


def test_dynamic_web_evidence_survives_final_validation_and_trace():
    fact = {
        "id": "web.4.8.0",
        "text": "Reviewed public website fact.",
        "source": "https://example.com/page#revision-8",
        "branches": [],
    }
    captured = {}

    def model_call(packet):
        captured.update(packet["knowledge"])
        return EvaluationDecision(
            action="reply",
            branch="unclassified",
            intent="other",
            reply="Reviewed public website fact.",
            evidence_refs=[fact["id"]],
        ), [], "digest"

    decision, _, _, trace = generate_decision({
        "customer_text": "What does the public website say?",
        "context_messages": [],
        "global_knowledge_facts": [fact],
        "global_knowledge_version": "revision-set-8",
    }, model_call=model_call)

    assert decision.evidence_refs == [fact["id"]]
    assert captured["version"].endswith("web:revision-set-8")
    assert trace["knowledge_version"].endswith("web:revision-set-8")
    assert trace["knowledge_usage"] == {
        "status": "used",
        "active": True,
        "version": "revision-set-8",
        "retrieved_fact_count": 1,
        "retrieved_fact_ids": [fact["id"]],
        "used_fact_count": 1,
        "used_fact_ids": [fact["id"]],
        "source_urls": [fact["source"]],
        "verification_passed": None,
        "response_source": "none",
    }


def test_reviewed_web_fact_resolves_unfamiliar_direct_question():
    fact = {
        "id": "web.1.2.0",
        "text": "China2Go is part of the CITS inbound travel service system.",
        "source": "https://china2go.com/cits-china2go/",
        "branches": [],
    }
    context = {
        "customer_text": "What is the relationship between China2Go and CITS?",
        "context_messages": [], "route_variant": "", "memory": {},
        "journey": {"sent_content_groups": [], "customer_profile": {}},
        "lead_capture": {"status": "not_started"}, "available_materials": [],
        "reception_policy": JOURNEY_POLICY, "global_knowledge_facts": [fact],
    }
    understanding = CustomerUnderstanding(
        intent="other", customer_questions=["other"],
        semantic_signals=["unresolved_direct_question"], confidence=0.9,
    )
    context["question_details"] = [{"topic": "other", "question": context["customer_text"], "evidence_quote": context["customer_text"]}]
    plan = build_reply_plan(context, understanding)
    assert plan.allowed_fact_ids == [fact["id"]]
    assert "unresolved_question_requires_clarification" not in plan.safety_flags
    assert plan.follow_up is None


def test_usage_endpoint_reports_each_persisted_reply(authenticated, session_factory):
    client, _ = authenticated
    with session_factory() as db:
        source = WebKnowledgeSource(
            tenant_id=1, name="Global knowledge", url="https://china2go.com/",
            status="paused", ai_enabled=False,
        )
        db.add(source)
        db.flush()
        session = AutomationSession(owner_id=1, environment="playground", mode="reply")
        db.add(session)
        db.flush()
        db.add(AutomationRun(
            session_id=session.id, module="reply", generation=1,
            idempotency_key="knowledge-usage-test", status="completed",
            trace={"knowledge_usage": {
                "status": "used", "retrieved_fact_count": 2,
                "retrieved_fact_ids": ["web.1.2.0", "web.1.2.1"],
                "used_fact_count": 1, "used_fact_ids": ["web.1.2.0"],
                "verification_passed": True, "response_source": "model",
            }},
        ))
        db.commit()
        source_id = source.id

    response = client.get(f"/v1/knowledge/web-sources/{source_id}/usage")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "total": 1, "used": 1, "retrieved_not_used": 0,
        "no_match": 0, "disabled": 0,
    }
    assert payload["items"][0]["environment"] == "playground"
    assert payload["items"][0]["usage"]["used_fact_ids"] == ["web.1.2.0"]


def test_refresh_and_publish_endpoints_default_to_disabled(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    monkeypatch.setattr("app.automation_api.normalize_public_url", lambda value: value.strip())
    created = client.post("/v1/knowledge/web-sources", headers={"X-CSRF-Token": csrf}, json={
        "name": "Official website",
        "url": "https://china2go.com/",
    })
    assert created.status_code == 200
    source_id = created.json()["id"]
    response = client.post(f"/v1/knowledge/web-sources/{source_id}/refresh", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "website_refresh_temporarily_disabled"


def test_curated_install_never_publishes_and_legacy_raw_content_is_not_retrieved(session_factory):
    payload = {
        "modules": [{
            "kind": "knowledge_module",
            "key": "transport",
            "title": "Transport",
            "summary": "Train booking",
            "topics": ["train", "ticket"],
            "facts": [{
                "text": "Advisors can help with train ticket booking.",
                "source_url": "https://china2go.com/faq-china/",
                "verified_at": "2026-09-10",
            }],
        }],
        "site_inventory": [],
        "excluded_items": [],
    }
    with session_factory() as db:
        source, revision = install_curated_global_library(db, payload, tenant_id=1, user_id=1)
        db.commit()
        assert source.url == "https://china2go.com/"
        assert source.ai_enabled is False
        assert source.published_revision_id is None
        assert revision.status == "pending_review"
        revision.status = "active"
        source.status = "active"
        source.ai_enabled = True
        source.runtime_scope = "live"
        source.published_revision_id = revision.id
        db.commit()
        facts, _ = active_web_facts(db, 1, "Can you help book a train ticket?")
        assert [item["text"] for item in facts] == ["Advisors can help with train ticket booking."]


def test_curated_candidate_preserves_current_publication_until_explicit_publish(session_factory):
    from copy import deepcopy
    from app.web_knowledge import publish_revision
    payload={'modules':[{'kind':'knowledge_module','key':'contact','title':'Contact','topics':['contact'],
        'facts':[{'text':'Approved contact A','source_url':'https://example.test/contact'}]}]}
    with session_factory() as db:
        source,previous=install_curated_global_library(db,payload,tenant_id=1,user_id=1)
        publish_revision(db,source,previous,runtime_scope='live')
        db.commit()
        published_at=previous.published_at
        _,same=install_curated_global_library(db,payload,tenant_id=1,user_id=1)
        assert same.id==previous.id and same.status=='active' and same.published_at==published_at
        changed=deepcopy(payload)
        changed['modules'][0]['facts'][0]['text']='Candidate contact B'
        source,candidate=install_curated_global_library(db,changed,tenant_id=1,user_id=1)
        db.commit()
        assert source.status=='active' and source.ai_enabled and source.runtime_scope=='live'
        assert source.published_revision_id==previous.id and previous.status=='active'
        assert candidate.status=='pending_review' and candidate.published_at is None
        assert [f['text'] for f in active_web_facts(db,1,'contact',candidate_pool=True)[0]]==['Approved contact A']
        publish_revision(db,source,candidate,runtime_scope='live')
        db.commit()
        assert previous.status=='superseded' and source.published_revision_id==candidate.id
        assert [f['text'] for f in active_web_facts(db,1,'contact',candidate_pool=True)[0]]==['Candidate contact B']


def test_curated_import_does_not_overwrite_an_unrelated_source(session_factory):
    with session_factory() as db:
        other=WebKnowledgeSource(tenant_id=1,name='Other approved website',url='https://example.test/',
            status='active',ai_enabled=True,runtime_scope='live',auth_type='wordpress_post_password',auth_secret=b'test-only')
        db.add(other);db.commit()
        source,_=install_curated_global_library(db,{'modules':[{'kind':'knowledge_module','key':'contact',
            'facts':[{'text':'Contact information'}]}]},tenant_id=1,user_id=1)
        db.commit();db.refresh(other)
        assert source.id!=other.id
        assert (other.name,other.url,other.status,other.auth_secret)==(
            'Other approved website','https://example.test/','active',b'test-only')
