from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import SessionLocal
from app.models import Tenant
from app.web_knowledge import extract_readable_content, install_curated_global_library, publish_revision


SITEMAP = "https://china2go.com/sitemap_index.xml"
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
CURATED = ROOT / "data" / "knowledge" / "china2go" / "global-website-knowledge" / "curated-modules.json"
DEFAULT_OUTPUT = ROOT / "output" / "global-website-knowledge-inventory.json"
REVIEWED_LIBRARY = ROOT / "data" / "knowledge" / "china2go" / "global-website-knowledge" / "reviewed-library.json"


def normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("－", "-").replace("–", "-")


def sitemap_urls(client: httpx.Client) -> list[tuple[str, str]]:
    index = ET.fromstring(client.get(SITEMAP).raise_for_status().content)
    inventories: list[tuple[str, str]] = []
    for node in index.findall(".//s:loc", NS):
        sitemap_url = str(node.text or "")
        kind = sitemap_url.rsplit("/", 1)[-1].replace("-sitemap.xml", "")
        sitemap = ET.fromstring(client.get(sitemap_url).raise_for_status().content)
        for url_node in sitemap.findall(".//s:loc", NS):
            url = str(url_node.text or "")
            if "/en/" in url or "/zh-hans/" in url:
                continue
            inventories.append((kind, url))
    return list(dict.fromkeys(inventories))


def crawl() -> tuple[list[dict], dict[str, str]]:
    inventory: list[dict] = []
    content_by_url: dict[str, str] = {}
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "China2GoKnowledgeAudit/1.0"}) as client:
        targets = sitemap_urls(client)

        def fetch(target: tuple[str, str]) -> tuple[dict, str]:
            kind, url = target
            try:
                response = client.get(url)
                response.raise_for_status()
                title, content = extract_readable_content(response.text, "text/html")
                return ({
                    "url": url,
                    "title": title,
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "content_length": len(content),
                    "site_section": kind,
                    "global_candidate": kind == "page" and url in {
                        "https://china2go.com/",
                        "https://china2go.com/cits-china2go/",
                        "https://china2go.com/faq-china/",
                        "https://china2go.com/faq-cits/",
                        "https://china2go.com/faq-tibet/",
                        "https://china2go.com/contact-cits/",
                        "https://china2go.com/cits-international-guests/",
                        "https://china2go.com/cits-china-branches/",
                    },
                    "status": "fetched",
                }, content)
            except Exception as exc:
                return ({"url": url, "site_section": kind, "global_candidate": False, "status": "failed", "error": type(exc).__name__}, "")

        with ThreadPoolExecutor(max_workers=12) as pool:
            for item, content in pool.map(fetch, targets):
                inventory.append(item)
                if content:
                    content_by_url[item["url"]] = content
    return inventory, content_by_url


def validate_sources(payload: dict, content_by_url: dict[str, str]) -> None:
    failures = []
    for module in payload["modules"]:
        for fact in module.get("facts") or []:
            url = fact["source_url"]
            content = content_by_url.get(url, "")
            if not content or normalized(fact["source_quote"]) not in normalized(content):
                failures.append({"module": module["key"], "url": url, "quote": fact["source_quote"]})
    if failures:
        raise SystemExit("source validation failed:\n" + json.dumps(failures, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--install-reviewed", action="store_true")
    parser.add_argument("--activate-playground", action="store_true")
    args = parser.parse_args()
    if args.install_reviewed:
        payload = json.loads(REVIEWED_LIBRARY.read_text(encoding="utf-8"))
        with SessionLocal() as db:
            tenant = db.query(Tenant).order_by(Tenant.id).first()
            if tenant is None:
                raise SystemExit("tenant_not_found")
            source, revision = install_curated_global_library(db, payload, tenant_id=tenant.id, user_id=1)
            if args.activate_playground:
                publish_revision(db, source, revision, runtime_scope="playground")
            db.commit()
            print(json.dumps({"source_id": source.id, "revision_id": revision.id, "runtime_scope": source.runtime_scope, **payload["crawl_summary"]}, ensure_ascii=False))
        return
    payload = json.loads(CURATED.read_text(encoding="utf-8"))
    inventory, content_by_url = crawl()
    validate_sources(payload, content_by_url)
    payload["site_inventory"] = inventory
    payload["crawl_summary"] = {
        "total_traditional_chinese_urls": len(inventory),
        "fetched": sum(item["status"] == "fetched" for item in inventory),
        "failed": sum(item["status"] == "failed" for item in inventory),
        "global_candidate_pages": sum(bool(item.get("global_candidate")) for item in inventory),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.install:
        with SessionLocal() as db:
            tenant = db.query(Tenant).order_by(Tenant.id).first()
            if tenant is None:
                raise SystemExit("tenant_not_found")
            source, revision = install_curated_global_library(db, payload, tenant_id=tenant.id, user_id=1)
            db.commit()
            print(json.dumps({"source_id": source.id, "revision_id": revision.id, **payload["crawl_summary"]}, ensure_ascii=False))
    else:
        print(json.dumps(payload["crawl_summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
