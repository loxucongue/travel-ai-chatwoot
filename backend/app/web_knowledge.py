"""Reviewed, tenant-scoped website knowledge with SSRF-safe manual refresh."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import WebKnowledgeRevision, WebKnowledgeSource, utcnow
from app.security import decrypt_secret


MAX_RESPONSE_BYTES = 2_500_000
MAX_REDIRECTS = 4
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
TRANSPARENT_PROXY_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")
BLOCKED_TAGS = {"script", "style", "noscript", "svg", "canvas", "form", "nav", "header", "footer", "aside"}
BLOCK_TAGS = {"title", "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "dt", "dd", "td", "th", "blockquote"}


def web_knowledge_refresh_enabled() -> bool:
    """Manual website crawling is disabled until the curated workflow is approved."""
    return os.getenv("WEB_KNOWLEDGE_REFRESH_ENABLED", "false").strip().lower() == "true"


def web_knowledge_publish_enabled() -> bool:
    """Publishing is a separate gate from crawling and defaults to disabled."""
    return os.getenv("WEB_KNOWLEDGE_PUBLISH_ENABLED", "false").strip().lower() == "true"


class WebKnowledgeError(ValueError):
    pass


def _is_cloudflare_challenge(title: str, content: str) -> bool:
    value = f"{title}\n{content}".lower()
    return any(marker in value for marker in (
        "just a moment",
        "performing security verification",
        "performance and security by cloudflare",
        "ray id:",
        "cf-chl-",
    ))


@dataclass(frozen=True)
class FetchedPage:
    title: str
    final_url: str
    content: str
    content_hash: str
    script_blocks: list[dict] = field(default_factory=list)
    image_candidates: list[dict] = field(default_factory=list)


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ignored_depth = 0
        self.current_tag = ""
        self.title_parts: list[str] = []
        self.blocks: list[str] = []
        self.buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in BLOCKED_TAGS:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag in BLOCK_TAGS:
            self._flush()
            self.current_tag = tag
        elif tag == "br":
            self.buffer.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in BLOCKED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag in BLOCK_TAGS:
            self._flush()
            self.current_tag = ""

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        value = re.sub(r"\s+", " ", unescape(data)).strip()
        if not value:
            return
        self.buffer.append(value)
        if self.current_tag == "title":
            self.title_parts.append(value)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        value = re.sub(r"[ \t]+", " ", " ".join(self.buffer))
        value = re.sub(r"\s*\n\s*", "\n", value).strip()
        self.buffer = []
        if len(value) < 2:
            return
        if not self.blocks or self.blocks[-1] != value:
            self.blocks.append(value)


def normalize_public_url(raw_url: str) -> str:
    value = raw_url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise WebKnowledgeError("website_url_must_be_public_https")
    if parsed.username or parsed.password or parsed.fragment:
        raise WebKnowledgeError("website_url_credentials_or_fragment_not_allowed")
    if parsed.port not in (None, 443):
        raise WebKnowledgeError("website_url_port_not_allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise WebKnowledgeError("website_url_private_host_not_allowed")
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise WebKnowledgeError("website_url_private_host_not_allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise WebKnowledgeError("website_url_dns_failed") from exc
    if not addresses:
        raise WebKnowledgeError("website_url_dns_failed")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        # Some managed desktop networks map public hostnames into the IANA
        # benchmarking range and route them through a transparent proxy.
        # The range is not usable for ordinary private services; all actual
        # loopback, RFC1918 and link-local addresses remain blocked.
        if literal_ip is None and ip in TRANSPARENT_PROXY_FAKE_IP:
            continue
        if not ip.is_global:
            raise WebKnowledgeError("website_url_private_host_not_allowed")
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def extract_readable_content(body: str, content_type: str) -> tuple[str, str]:
    if content_type.startswith("text/plain"):
        text = re.sub(r"\r\n?", "\n", body)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return "", text
    parser = _ReadableHTML()
    parser.feed(body)
    parser.close()
    content = "\n\n".join(parser.blocks).strip()
    title = " ".join(parser.title_parts).strip()
    return title[:500], content


def fetch_website(url: str, *, client: httpx.Client | None = None) -> FetchedPage:
    current = normalize_public_url(url)
    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=False)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            with http.stream("GET", current, headers={"User-Agent": "China2GoKnowledgeBot/1.0"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise WebKnowledgeError("website_redirect_missing_location")
                    current = normalize_public_url(urljoin(current, location))
                    continue
                if response.status_code >= 400:
                    raise WebKnowledgeError(f"website_http_error:{response.status_code}")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if not any(content_type.startswith(item) for item in ALLOWED_CONTENT_TYPES):
                    raise WebKnowledgeError("website_content_type_not_supported")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise WebKnowledgeError("website_content_too_large")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                body = b"".join(chunks).decode(encoding, errors="replace")
                if "name=\"post_password\"" in body or "name='post_password'" in body:
                    raise WebKnowledgeError("website_password_required")
                title, content = extract_readable_content(body, content_type)
                if len(content) < 80:
                    raise WebKnowledgeError("website_readable_content_too_short")
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                return FetchedPage(title=title, final_url=current, content=content, content_hash=digest)
        raise WebKnowledgeError("website_too_many_redirects")
    except httpx.HTTPError as exc:
        raise WebKnowledgeError("website_fetch_failed") from exc
    finally:
        if owns_client:
            http.close()


def fetch_protected_website(url: str, password: str) -> FetchedPage:
    """Fetch a password-protected WordPress page in an ephemeral browser context."""
    normalized = normalize_public_url(url)
    expected_host = urlsplit(normalized).hostname
    if not password:
        raise WebKnowledgeError("website_password_required")
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise WebKnowledgeError("website_browser_unavailable") from exc
    try:
        with sync_playwright() as runtime:
            executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or None
            browser = runtime.chromium.launch(headless=True, executable_path=executable)
            context = browser.new_context(locale="zh-TW")
            page = context.new_page()

            def restrict_route(route) -> None:
                target = urlsplit(route.request.url)
                host = (target.hostname or "").lower()
                if target.scheme not in {"https", "data", "blob"}:
                    route.abort()
                    return
                if target.scheme == "https" and host != expected_host and not host.endswith(".cloudflare.com"):
                    route.abort()
                    return
                route.continue_()

            page.route("**/*", restrict_route)
            page.goto(normalized, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_function(
                    """() => {
                      const title = (document.title || '').toLowerCase();
                      const text = (document.body?.innerText || '').toLowerCase();
                      return !title.includes('just a moment')
                        && !text.includes('performing security verification')
                        && !text.includes('performance and security by cloudflare')
                        && !text.includes('ray id:');
                    }""",
                    timeout=90_000,
                )
            except PlaywrightTimeoutError as exc:
                raise WebKnowledgeError("website_cloudflare_challenge") from exc
            password_input = page.locator('input[name="post_password"]')
            if password_input.count():
                password_input.fill(password)
                submit = password_input.locator("xpath=ancestor::form").locator('input[type="submit"], button[type="submit"]').first
                with page.expect_navigation(wait_until="domcontentloaded", timeout=60_000):
                    submit.click()
                try:
                    page.wait_for_function(
                        """() => {
                          const title = (document.title || '').toLowerCase();
                          const text = (document.body?.innerText || '').toLowerCase();
                          return !title.includes('just a moment')
                            && !text.includes('performing security verification')
                            && !text.includes('performance and security by cloudflare')
                            && !text.includes('ray id:');
                        }""",
                        timeout=90_000,
                    )
                except PlaywrightTimeoutError as exc:
                    raise WebKnowledgeError("website_cloudflare_challenge") from exc
                if page.locator('input[name="post_password"]').count():
                    raise WebKnowledgeError("website_password_invalid")
                page.wait_for_function(
                    "document.title && !document.title.toLowerCase().startsWith('loading') && (document.body?.innerText || '').length > 80",
                    timeout=60_000,
                )
            if page.locator('input[name="post_password"]').count():
                raise WebKnowledgeError("website_password_invalid")
            if (urlsplit(page.url).hostname or "").lower() != expected_host:
                raise WebKnowledgeError("website_redirect_host_not_allowed")
            page.evaluate("""async () => {
              document.querySelectorAll('details').forEach(node => { node.open = true; });
              for (let y = 0; y < document.body.scrollHeight; y += 800) {
                window.scrollTo(0, y);
                await new Promise(resolve => setTimeout(resolve, 40));
              }
              window.scrollTo(0, 0);
            }""")
            extracted = page.evaluate("""() => ({
              title: document.title || '',
              text: (document.body?.innerText || '').replace(/\\n{3,}/g, '\\n\\n').trim(),
              scripts: Array.from(document.querySelectorAll('details')).map((node, index) => ({
                index: index + 1,
                title: (node.querySelector('summary')?.innerText || '').trim(),
                content: (node.textContent || '').trim()
              })).filter(item => item.content),
              images: Array.from(document.images).map(img => ({
                url: img.currentSrc || img.src || '', alt: img.alt || '',
                width: img.naturalWidth || 0, height: img.naturalHeight || 0
              })).filter(item => item.url)
            })""")
            context.close()
            browser.close()
    except WebKnowledgeError:
        raise
    except PlaywrightTimeoutError as exc:
        raise WebKnowledgeError("website_browser_timeout") from exc
    except Exception as exc:
        raise WebKnowledgeError("website_browser_fetch_failed") from exc
    content = str(extracted.get("text") or "").strip()
    if _is_cloudflare_challenge(str(extracted.get("title") or ""), content):
        raise WebKnowledgeError("website_cloudflare_challenge")
    if len(content) < 80:
        raise WebKnowledgeError("website_readable_content_too_short")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    images = []
    seen = set()
    for item in extracted.get("images") or []:
        image_url = str(item.get("url") or "")
        key = hashlib.sha256(image_url.encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        width, height = int(item.get("width") or 0), int(item.get("height") or 0)
        images.append({**item, "content_key": key, "quality": "low_resolution" if max(width, height) < 744 else "review"})
    return FetchedPage(
        title=str(extracted.get("title") or "")[:500], final_url=normalized,
        content=content, content_hash=digest,
        script_blocks=list(extracted.get("scripts") or []), image_candidates=images,
    )


def refresh_source(db: Session, source: WebKnowledgeSource, user_id: int | None) -> tuple[WebKnowledgeRevision, bool]:
    source.sync_status = "syncing"
    source.last_error = None
    source.last_checked_at = utcnow()
    db.flush()
    try:
        try:
            page = fetch_website(source.url)
        except WebKnowledgeError as exc:
            if source.auth_type != "wordpress_post_password" or not source.auth_secret or str(exc) not in {
                "website_password_required", "website_http_error:403", "website_fetch_failed"
            }:
                raise
            page = fetch_protected_website(source.url, decrypt_secret(source.auth_secret))
        existing = db.scalar(select(WebKnowledgeRevision).where(
            WebKnowledgeRevision.source_id == source.id,
            WebKnowledgeRevision.content_hash == page.content_hash,
        ))
        if existing:
            source.latest_revision_id = existing.id
            source.sync_status = "ready"
            source.updated_at = utcnow()
            return existing, False
        number = int(db.scalar(select(func.max(WebKnowledgeRevision.revision_number)).where(
            WebKnowledgeRevision.source_id == source.id
        )) or 0) + 1
        revision = WebKnowledgeRevision(
            source_id=source.id,
            revision_number=number,
            title=page.title or source.name,
            final_url=page.final_url,
            content=page.content,
            content_hash=page.content_hash,
            content_length=len(page.content),
            script_blocks=page.script_blocks,
            image_candidates=page.image_candidates,
            status="pending_review",
            created_by=user_id,
        )
        db.add(revision)
        db.flush()
        source.latest_revision_id = revision.id
        source.sync_status = "ready"
        source.last_changed_at = utcnow()
        source.updated_at = utcnow()
        return revision, True
    except WebKnowledgeError as exc:
        source.sync_status = "failed"
        source.last_error = str(exc)
        source.updated_at = utcnow()
        raise


def publish_revision(
    db: Session,
    source: WebKnowledgeSource,
    revision: WebKnowledgeRevision,
    *,
    runtime_scope: str = "playground",
) -> None:
    if runtime_scope not in {"playground", "live"}:
        raise WebKnowledgeError("website_runtime_scope_invalid")
    if revision.source_id != source.id:
        raise WebKnowledgeError("website_revision_source_mismatch")
    previous = db.scalar(select(WebKnowledgeRevision).where(
        WebKnowledgeRevision.id == source.published_revision_id
    )) if source.published_revision_id else None
    if previous and previous.id != revision.id:
        previous.status = "superseded"
    revision.status = "active"
    revision.published_at = utcnow()
    source.published_revision_id = revision.id
    source.ai_enabled = True
    source.runtime_scope = runtime_scope
    source.status = "active"
    source.updated_at = utcnow()


def fact_answer_requirements(fact: dict) -> list[dict]:
    """Bounded literal conditions from reviewed knowledge, never executable patterns."""
    requirements = fact.get('answer_requirements', [])
    if not isinstance(requirements, list) or len(requirements) > 8:
        raise WebKnowledgeError('website_fact_answer_requirements_invalid')
    for item in requirements:
        if (not isinstance(item, dict) or not isinstance(item.get('label'), str)
                or not 1 <= len(item['label']) <= 160
                or not isinstance(item.get('any_of'), list) or not 1 <= len(item['any_of']) <= 12
                or any(not isinstance(value, str) or not value.strip() or len(value) > 160
                       for value in item['any_of'])):
            raise WebKnowledgeError('website_fact_answer_requirements_invalid')
    return requirements


def install_curated_global_library(
    db: Session,
    payload: dict,
    *,
    tenant_id: int,
    user_id: int | None = None,
) -> tuple[WebKnowledgeSource, WebKnowledgeRevision]:
    """Install a reviewed candidate without exposing it to AI."""
    modules = payload.get("modules") or []
    if not modules or any(item.get("kind") != "knowledge_module" for item in modules):
        raise WebKnowledgeError("website_curated_modules_invalid")
    for module in modules:
        for fact in module.get('facts') or []:
            fact_answer_requirements(fact)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    source = db.scalar(select(WebKnowledgeSource).where(
        WebKnowledgeSource.tenant_id == tenant_id,
        WebKnowledgeSource.name == "China2Go 官網全局知識",
        WebKnowledgeSource.url == "https://china2go.com/",
        WebKnowledgeSource.status != "archived",
    ).order_by(WebKnowledgeSource.id))
    if source is None:
        source = WebKnowledgeSource(tenant_id=tenant_id, name="China2Go 官網全局知識", url="https://china2go.com/")
        db.add(source)
        db.flush()
    source.name = "China2Go 官網全局知識"
    source.url = "https://china2go.com/"
    source.description = "從 China2Go 繁體中文官網整理，供全部產品線共用的文字知識。"
    source.match_keywords = sorted({
        topic
        for module in modules
        for topic in module.get("topics") or []
        if isinstance(topic, str) and topic.strip()
    })
    source.auth_type = "none"
    source.auth_secret = None
    if not source.published_revision_id:
        source.status = "paused"
        source.ai_enabled = False
        source.runtime_scope = "disabled"
    source.sync_status = "ready"
    source.last_error = None
    source.last_checked_at = utcnow()
    source.last_changed_at = utcnow()
    source.updated_at = utcnow()
    existing = db.scalar(select(WebKnowledgeRevision).where(
        WebKnowledgeRevision.source_id == source.id,
        WebKnowledgeRevision.content_hash == digest,
    ))
    if existing is None:
        number = int(db.scalar(select(func.max(WebKnowledgeRevision.revision_number)).where(
            WebKnowledgeRevision.source_id == source.id
        )) or 0) + 1
        existing = WebKnowledgeRevision(
            source_id=source.id,
            revision_number=number,
            title="China2Go 官網全局知識",
            final_url="https://china2go.com/",
            content=serialized,
            content_hash=digest,
            content_length=sum(len(str(fact.get("text") or "")) for module in modules for fact in module.get("facts") or []),
            status="pending_review",
            created_by=user_id,
            script_blocks=modules,
            image_candidates=[],
        )
        db.add(existing)
        db.flush()
    for revision in db.scalars(select(WebKnowledgeRevision).where(
        WebKnowledgeRevision.source_id == source.id,
        WebKnowledgeRevision.id != existing.id,
        WebKnowledgeRevision.id != (source.published_revision_id or -1),
        WebKnowledgeRevision.status != "superseded",
    )).all():
        revision.status = "superseded"
    if existing.id != source.published_revision_id:
        existing.status = "pending_review"
        existing.published_at = None
    source.latest_revision_id = existing.id
    return source, existing


def _tokens(value: str) -> set[str]:
    lowered = value.lower()
    latin = set(re.findall(r"[a-z0-9]{2,}", lowered))
    normalized = re.sub(r"\s+", "", lowered)
    chinese = re.sub(r"[^\u3400-\u9fff]", "", normalized)
    return latin | {chinese[i:i + 2] for i in range(max(0, len(chinese) - 1))}


def _chunks(content: str, limit: int = 900) -> list[str]:
    parts = [item.strip() for item in re.split(r"\n{2,}", content) if item.strip()]
    chunks: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) + 2 > limit:
            chunks.append(current)
            current = ""
        if len(part) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(part[i:i + limit] for i in range(0, len(part), limit))
        else:
            current = f"{current}\n\n{part}".strip()
    if current:
        chunks.append(current)
    return chunks


def revision_knowledge_modules(revision: WebKnowledgeRevision) -> list[dict]:
    """Return only reviewed, structured modules; legacy scraped blocks are ignored."""
    modules = []
    for item in revision.script_blocks or []:
        if not isinstance(item, dict) or item.get("kind") != "knowledge_module":
            continue
        facts = [fact for fact in item.get("facts") or [] if isinstance(fact, dict) and fact.get("text")]
        modules.append({**item, "facts": facts})
    return modules


def active_web_facts(
    db: Session,
    tenant_id: int | None,
    customer_text: str,
    *,
    environment: str = "live",
    limit: int = 4,
    candidate_pool: bool = False,
) -> tuple[list[dict], str]:
    if tenant_id is None or not customer_text.strip():
        return [], ""
    rows = db.execute(
        select(WebKnowledgeSource, WebKnowledgeRevision)
        .join(WebKnowledgeRevision, WebKnowledgeRevision.id == WebKnowledgeSource.published_revision_id)
        .where(
            WebKnowledgeSource.tenant_id == tenant_id,
            WebKnowledgeSource.status == "active",
            WebKnowledgeSource.ai_enabled.is_(True),
            WebKnowledgeSource.runtime_scope.in_(["playground", "live"] if environment == "playground" else ["live"]),
            WebKnowledgeRevision.status == "active",
        )
    ).all()
    query_tokens = _tokens(customer_text)
    scored: list[tuple[float, dict]] = []
    versions: list[str] = []
    for source, revision in rows:
        source_tokens = _tokens(" ".join([source.name, source.description, *source.match_keywords]))
        versions.append(f"{source.id}:{revision.content_hash[:12]}")
        modules = revision_knowledge_modules(revision)
        for module_index, module in enumerate(modules):
            module_tokens = _tokens(" ".join([
                str(module.get("title") or ""),
                str(module.get("summary") or ""),
                *[str(topic) for topic in module.get("topics") or []],
            ]))
            for fact_index, fact in enumerate(module.get("facts") or []):
                text = str(fact.get("text") or "").strip()
                fact_overlap = query_tokens & _tokens(text)
                module_overlap = query_tokens & module_tokens
                source_overlap = query_tokens & source_tokens
                if not candidate_pool and not fact_overlap and not module_overlap:
                    continue
                score = float(len(fact_overlap) * 3 + len(module_overlap) * 2)
                scored.append((score, {
                    "id": f"web.{source.id}.{revision.id}.{module_index}.{fact_index}",
                    "branches": [],
                    "source": str(fact.get("source_url") or revision.final_url),
                    "text": text,
                    "source_name": source.name,
                    "module_key": str(module.get("key") or ""),
                    "topics": module.get("topics") or [],
                    "answer_requirements": fact_answer_requirements(fact),
                    "verified_at": str(fact.get("verified_at") or revision.fetched_at),
                }))
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    facts = [item[1] for item in (scored if candidate_pool else scored[:limit])]
    version = hashlib.sha256("|".join(sorted(versions)).encode()).hexdigest()[:16] if versions else ""
    return facts, version


def context_fact_map(context: dict) -> dict[str, dict]:
    combined = {}
    for fact in context.get("global_knowledge_facts") or []:
        if isinstance(fact, dict) and str(fact.get("id") or "").startswith("web.") and fact.get("text"):
            combined[str(fact["id"])] = fact
    return combined


def enrich_context_with_web_knowledge(
    db: Session,
    tenant_id: int | None,
    context: dict,
    *,
    environment: str = "live",
) -> dict:
    facts, version = active_web_facts(
        db, tenant_id, str(context.get("customer_text") or ""), environment=environment
    )
    candidates, _ = active_web_facts(db, tenant_id, str(context.get("customer_text") or ""),
                                    environment=environment, candidate_pool=True)
    return {
        **context,
        "global_knowledge_facts": facts,
        "global_knowledge_version": version,
        "global_knowledge_candidates": candidates,
        "environment": environment,
    }


def select_understood_web_facts(context: dict, understanding) -> dict:
    """Select reviewed evidence after resolving the customer's actual question."""
    modules = {
        "contact": {"official_contact"},
        "company": {"brand_and_identity", "official_contact"},
        "transport": {"transport_and_transfers"},
        "payment": {"booking_payment_and_changes", "travel_preparation"},
        "oxygen_service": {"accommodation_oxygen_and_health"},
        "eligibility": {"booking_payment_and_changes", "tibet_permit"},
        "price": {"booking_payment_and_changes", "advisor_and_trip_planning"},
        "booking": {"booking_payment_and_changes"},
        "availability": {"booking_payment_and_changes"},
        "party_size": {"advisor_and_trip_planning", "booking_payment_and_changes"},
        "documents": {"tibet_permit"},
        "hotel": {"accommodation_oxygen_and_health"},
        "rongbuk": {"accommodation_oxygen_and_health"},
        "vehicle": {"transport_and_transfers", "accommodation_oxygen_and_health"},
        "medical_service": {"accommodation_oxygen_and_health"},
        "altitude_health": {"accommodation_oxygen_and_health"},
        "weather": {"travel_preparation"},
        "departure": {"travel_preparation", "booking_payment_and_changes"},
    }
    details = understanding.question_details
    topics = set(understanding.customer_questions)
    allowed_modules = set().union(*(modules.get(topic, set()) for topic in topics))
    query = " ".join([understanding.discussion_subject,
                      *[item["question"] for item in details]]) or str(context.get("customer_text") or "")
    tokens = _tokens(query)
    pool = context.get("global_knowledge_candidates", context.get("global_knowledge_facts", []))
    selected, rejected = [], []
    for fact in pool:
        key = fact.get("module_key", "")
        overlap = tokens & _tokens(str(fact.get("text") or ""))
        # Unknown topics require actual paragraph evidence, never source-wide keywords.
        eligible = key in allowed_modules if topics - {"other"} else bool(overlap)
        if topics == {"contact"}:
            eligible = key == "official_contact"
        if eligible and (overlap or key == "official_contact"):
            selected.append((len(overlap), fact))
        else:
            rejected.append({"fact_id": fact["id"], "reason": "question_topic_mismatch"})
    selected.sort(key=lambda item: (-item[0], item[1]["id"]))
    facts = [fact for _, fact in selected[:6]]
    return {**context, "global_knowledge_facts": facts,
            "question_details": details, "discussion_subject": understanding.discussion_subject,
            "knowledge_selection": {"topics": sorted(topics), "query": query,
                                    "selected_fact_ids": [f["id"] for f in facts],
                                    "rejected": rejected}}
