"""Refresh allowlisted China2Go service pages without activating new answers."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup


ROOT = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "knowledge"
    / "china2go"
    / "service-knowledge"
)
MANIFEST = ROOT / "service-knowledge.json"


def _page_markdown(url: str, html: str, fetched_at: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for selector in ("script", "style", "noscript", "svg", "form", "nav", "header", "footer"):
        for node in soup.select(selector):
            node.decompose()
    content = (
        soup.select_one("article .entry-content")
        or soup.select_one(".entry-content")
        or soup.select_one("main")
        or soup.select_one("article")
        or soup.body
    )
    if content is None:
        raise RuntimeError("service_page_content_missing")
    title = (soup.title.get_text(" ", strip=True) if soup.title else url).split("|")[0].strip()
    lines = []
    for raw in content.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return f"# {title}\n\nSource: {url}\nFetched: {fetched_at}\n\n" + "\n\n".join(lines) + "\n"


def main() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    headers = {"User-Agent": "China2GoKnowledgeSync/1.0 (+https://china2go.com/)"}
    with httpx.Client(headers=headers, timeout=60, follow_redirects=True) as client:
        for source in data.get("sources", []):
            url = str(source.get("url") or "")
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "china2go.com":
                raise RuntimeError(f"service_source_not_allowed:{url}")
            response = client.get(url)
            response.raise_for_status()
            markdown = _page_markdown(url, response.text, fetched_at)
            path = (ROOT / str(source["snapshot_path"])).resolve()
            if not path.is_relative_to(ROOT.resolve()):
                raise RuntimeError("service_snapshot_path_outside_root")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
            source["snapshot_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            source["fetched_at"] = fetched_at
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "knowledge_version": data.get("knowledge_version"),
        "sources": len(data.get("sources", [])),
        "fetched_at": fetched_at,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

