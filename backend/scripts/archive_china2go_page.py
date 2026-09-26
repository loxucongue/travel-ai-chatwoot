from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify


SOURCE_PAGE = "https://china2go.com/7693-2/"
WORDPRESS_API = "https://china2go.com/wp-json/wp/v2/posts"
DEFAULT_OUTPUT = Path("data/knowledge/china2go/website-7693-full")
PASSWORD_ENV = "CHINA2GO_PAGE_PASSWORD"


@dataclass(frozen=True)
class Category:
    number: int
    directory: str
    title: str
    kind: str = "business_branch"


CATEGORIES = (
    Category(1, "01-peach-everest-11d", "桃花＋珠峰 11日"),
    Category(2, "02-peach-9d", "桃花 9日"),
    Category(3, "03-tibet-overview-10d", "西藏全覽-10天林芝+拉薩+珠峰"),
    Category(4, "04-other-time-everest", "其他時間・上珠峰"),
    Category(5, "05-other-time-no-everest", "其他時間・不上珠峰"),
    Category(6, "06-private-group-or-other", "自己包團或其他地點"),
)

TOKEN_RE = re.compile(r"asset://occurrence-(\d{3})")
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE_RE = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive the password-protected China2Go business page by branch."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sanitize_filename(value: str, fallback: str) -> str:
    cleaned = INVALID_FILENAME_RE.sub("-", WHITESPACE_RE.sub(" ", value)).strip(" .-")
    return (cleaned or fallback)[:72]


def extension_for(mime_type: str, source: str) -> str:
    normalized = mime_type.split(";", 1)[0].lower()
    overrides = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }
    if normalized in overrides:
        return overrides[normalized]
    suffix = Path(urlparse(source).path).suffix.lower()
    if suffix and len(suffix) <= 6:
        return suffix
    return mimetypes.guess_extension(normalized) or ".bin"


def decode_image(client: httpx.Client, image: Any) -> tuple[bytes, str, str]:
    candidates = (
        image.get("data-orig-file"),
        image.get("data-large-file"),
        image.get("data-lazy-src"),
        image.get("data-src"),
        image.get("src"),
    )
    source = next((value for value in candidates if value), None)
    if not source:
        raise ValueError("image has no source")

    if source.startswith("data:"):
        header, encoded = source.split(",", 1)
        mime_type = header[5:].split(";", 1)[0] or "application/octet-stream"
        if ";base64" in header:
            payload = base64.b64decode(encoded)
        else:
            from urllib.parse import unquote_to_bytes

            payload = unquote_to_bytes(encoded)
        return payload, mime_type, "embedded:data-uri"

    absolute_url = urljoin(SOURCE_PAGE, source)
    response = client.get(absolute_url)
    response.raise_for_status()
    mime_type = response.headers.get("content-type", "application/octet-stream")
    return response.content, mime_type, absolute_url


def normalize_markdown(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").split("\n")]
    output: list[str] = []
    blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and blank:
            continue
        output.append(line)
        blank = is_blank
    return "\n".join(output).strip() + "\n"


def find_branch_ranges(markdown: str) -> tuple[str, dict[int, str]]:
    starts: list[tuple[int, Category]] = []
    for category in CATEGORIES:
        pattern = re.compile(
            rf"(?m)^\s*{category.number}\s*$\n\s*\n?\s*{re.escape(category.title)}\s*$"
        )
        match = pattern.search(markdown)
        if not match:
            raise ValueError(f"branch marker not found: {category.number} {category.title}")
        starts.append((match.start(), category))

    starts.sort(key=lambda item: item[0])
    preamble = markdown[: starts[0][0]].strip()
    branches: dict[int, str] = {}
    for index, (start, category) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(markdown)
        branches[category.number] = markdown[start:end].strip()
    return preamble, branches


def replace_asset_tokens(
    markdown: str,
    occurrences: dict[int, dict[str, Any]],
    destination: Path,
    path_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    hash_paths: dict[str, str] = {}
    sequence = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal sequence
        occurrence_id = int(match.group(1))
        item = occurrences[occurrence_id]
        asset_hash = item["sha256"]
        local_name = hash_paths.get(asset_hash)
        if local_name is None:
            sequence += 1
            label = sanitize_filename(item["alt"], f"image-{sequence:02d}")
            local_name = f"{sequence:02d}-{label}-{asset_hash[:8]}{item['extension']}"
            hash_paths[asset_hash] = local_name
            target = destination / local_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item["global_path"], target)
        selected.append(
            {
                "occurrence_id": occurrence_id,
                "alt": item["alt"],
                "sha256": asset_hash,
                "mime_type": item["mime_type"],
                "bytes": item["bytes"],
                "source": item["source"],
                "local_path": f"{path_prefix}/{local_name}",
            }
        )
        return f"{path_prefix}/{local_name}"

    return TOKEN_RE.sub(replace, markdown), selected


def prepare_output(output: Path, overwrite: bool) -> None:
    if not output.exists():
        output.mkdir(parents=True)
        return
    if not overwrite:
        raise FileExistsError(f"output already exists: {output}; pass --overwrite to replace it")
    resolved = output.resolve()
    if resolved.name != "website-7693-full" or "data" not in resolved.parts:
        raise ValueError(f"refusing to replace unexpected path: {resolved}")
    backup = resolved.with_name(f"{resolved.name}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    resolved.rename(backup)
    resolved.mkdir(parents=True)


def build_readme(
    archived_at: str,
    post: dict[str, Any],
    category_rows: list[dict[str, Any]],
    occurrence_count: int,
    unique_count: int,
) -> str:
    rows = "\n".join(
        f"| {row['number']} | {row['title']} | `{row['directory']}` | "
        f"{row['image_occurrences']} | {row['unique_images']} | {row['content_chars']} |"
        for row in category_rows
    )
    return f"""# China2Go 业务页面完整归档

- 来源：<{SOURCE_PAGE}>
- WordPress Post ID：`{post['id']}`
- 页面更新时间：`{post.get('modified_gmt') or post.get('modified')}`
- 本地归档时间（UTC）：`{archived_at}`
- 业务分支：`{len(CATEGORIES)}`
- 图片出现次数：`{occurrence_count}`
- 去重后图片：`{unique_count}`
- 页面密码：未保存

## 目录说明

- `source/content.html`：页面业务正文的原始 HTML，保留所有内嵌图片。
- `source/content.md`：全页可读 Markdown，图片改为本地去重素材路径。
- `source/images/`：全页按内容哈希去重的图片。
- `00-global-rules/`：进入各项目之前的全局价格、加价日期和春节规则。
- `01-*` 至 `06-*`：按页面六个业务分支拆分的文案、图片和清单。
- `manifest.json`：机器可读的总清单、哈希和图片出现位置。

## 项目分类

| 编号 | 页面项目 | 本地目录 | 图片出现次数 | 分支内去重图片 | 文案字符数 |
|---:|---|---|---:|---:|---:|
{rows}

## 使用边界

本目录是网站内容的资料归档，不代表其中的价格、日期、年龄、健康、证件、酒店供氧、赔偿等信息在当前仍然有效。将资料接入 AI 或 SOP 前，应由业务负责人审核并标注有效期；高风险和时效性事实不能直接作为模型自由回答依据。
"""


def main() -> int:
    args = parse_args()
    password = os.getenv(PASSWORD_ENV, "").strip()
    if not password:
        print(f"Missing required environment variable: {PASSWORD_ENV}", file=sys.stderr)
        return 2

    output = args.output.resolve()
    prepare_output(output, args.overwrite)
    archived_at = datetime.now(UTC).isoformat()

    headers = {"User-Agent": "China2GoKnowledgeArchiver/1.0"}
    with httpx.Client(headers=headers, timeout=60, follow_redirects=True) as client:
        response = client.get(
            WORDPRESS_API,
            params={"slug": "7693-2", "password": password, "per_page": 100},
        )
        response.raise_for_status()
        posts = response.json()
        if len(posts) != 1:
            raise RuntimeError(f"expected one WordPress post, received {len(posts)}")
        post = posts[0]
        raw_html = post["content"]["rendered"]
        soup = BeautifulSoup(raw_html, "html.parser")

        global_images = output / "source" / "images"
        global_images.mkdir(parents=True, exist_ok=True)
        occurrences: dict[int, dict[str, Any]] = {}
        unique_assets: dict[str, dict[str, Any]] = {}

        for occurrence_id, image in enumerate(soup.find_all("img"), start=1):
            payload, mime_type, source = decode_image(client, image)
            digest = hashlib.sha256(payload).hexdigest()
            extension = extension_for(mime_type, source)
            global_name = f"asset-{digest[:16]}{extension}"
            global_path = global_images / global_name
            if not global_path.exists():
                global_path.write_bytes(payload)
            alt = WHITESPACE_RE.sub(" ", image.get("alt") or "").strip()
            item = {
                "occurrence_id": occurrence_id,
                "alt": alt,
                "sha256": digest,
                "mime_type": mime_type.split(";", 1)[0],
                "bytes": len(payload),
                "source": source,
                "extension": extension,
                "global_path": global_path,
                "global_local_path": f"source/images/{global_name}",
            }
            occurrences[occurrence_id] = item
            unique_assets.setdefault(
                digest,
                {
                    "sha256": digest,
                    "mime_type": item["mime_type"],
                    "bytes": item["bytes"],
                    "local_path": item["global_local_path"],
                    "first_alt": alt,
                },
            )
            image["src"] = f"asset://occurrence-{occurrence_id:03d}"
            for attribute in (
                "srcset",
                "data-src",
                "data-lazy-src",
                "data-orig-file",
                "data-large-file",
                "data-srcset",
            ):
                image.attrs.pop(attribute, None)

    token_markdown = normalize_markdown(
        markdownify(str(soup), heading_style="ATX", bullets="-")
    )
    preamble, branch_markdown = find_branch_ranges(token_markdown)

    write_text(output / "source" / "content.html", raw_html)
    write_json(
        output / "source" / "post-metadata.json",
        {
            "id": post["id"],
            "date": post.get("date"),
            "date_gmt": post.get("date_gmt"),
            "modified": post.get("modified"),
            "modified_gmt": post.get("modified_gmt"),
            "slug": post.get("slug"),
            "status": post.get("status"),
            "type": post.get("type"),
            "link": post.get("link"),
            "title": post.get("title", {}).get("rendered"),
            "archived_at": archived_at,
            "password_stored": False,
        },
    )

    full_markdown = token_markdown
    for occurrence_id, item in occurrences.items():
        full_markdown = full_markdown.replace(
            f"asset://occurrence-{occurrence_id:03d}",
            f"images/{Path(item['global_local_path']).name}",
        )
    write_text(output / "source" / "content.md", full_markdown)

    preamble_with_assets, preamble_assets = replace_asset_tokens(
        preamble,
        occurrences,
        output / "00-global-rules" / "images",
        "images",
    )
    write_text(output / "00-global-rules" / "content.md", preamble_with_assets)
    write_json(
        output / "00-global-rules" / "manifest.json",
        {
            "kind": "global_rules",
            "title": "全局规则与入口说明",
            "content_chars": len(preamble_with_assets),
            "images": preamble_assets,
        },
    )

    category_rows: list[dict[str, Any]] = []
    occurrence_to_category: dict[int, str] = {
        item["occurrence_id"]: "00-global-rules" for item in preamble_assets
    }
    rendered_branches: dict[int, str] = {}
    for category in CATEGORIES:
        category_root = output / category.directory
        content, assets = replace_asset_tokens(
            branch_markdown[category.number],
            occurrences,
            category_root / "images",
            "images",
        )
        write_text(category_root / "content.md", content)
        rendered_branches[category.number] = content
        manifest_assets = []
        for item in assets:
            occurrence_to_category[item["occurrence_id"]] = category.directory
            manifest_assets.append(item)
        row = {
            "number": category.number,
            "kind": category.kind,
            "title": category.title,
            "directory": category.directory,
            "content_chars": len(content),
            "image_occurrences": len(assets),
            "unique_images": len({item["sha256"] for item in assets}),
        }
        category_rows.append(row)
        write_json(
            category_root / "manifest.json",
            {
                **row,
                "source_page": SOURCE_PAGE,
                "images": manifest_assets,
            },
        )

    unassigned = sorted(set(occurrences) - set(occurrence_to_category))
    occurrence_manifest = []
    for occurrence_id, item in occurrences.items():
        occurrence_manifest.append(
            {
                "occurrence_id": occurrence_id,
                "category": occurrence_to_category.get(occurrence_id),
                "alt": item["alt"],
                "sha256": item["sha256"],
                "mime_type": item["mime_type"],
                "bytes": item["bytes"],
                "source": item["source"],
                "global_local_path": item["global_local_path"],
            }
        )

    manifest = {
        "schema_version": 1,
        "source_page": SOURCE_PAGE,
        "wordpress_post_id": post["id"],
        "archived_at": archived_at,
        "password_stored": False,
        "raw_html_sha256": hashlib.sha256(raw_html.encode("utf-8")).hexdigest(),
        "content_counts": {
            "html_chars": len(raw_html),
            "markdown_chars": len(full_markdown),
            "details_sections": len(BeautifulSoup(raw_html, "html.parser").find_all("details")),
            "business_branches": len(CATEGORIES),
            "image_occurrences": len(occurrences),
            "unique_images": len(unique_assets),
        },
        "categories": category_rows,
        "unique_assets": list(unique_assets.values()),
        "image_occurrences": occurrence_manifest,
        "validation": {
            "unassigned_image_occurrences": unassigned,
            "all_tokens_replaced": "asset://" not in full_markdown
            and "asset://" not in preamble_with_assets
            and all("asset://" not in content for content in rendered_branches.values()),
        },
    }
    write_json(output / "manifest.json", manifest)
    write_text(
        output / "README.md",
        build_readme(
            archived_at,
            post,
            category_rows,
            len(occurrences),
            len(unique_assets),
        ),
    )

    if unassigned:
        raise RuntimeError(f"unassigned image occurrences: {unassigned}")
    print(
        json.dumps(
            {
                "output": str(output),
                "branches": len(CATEGORIES),
                "image_occurrences": len(occurrences),
                "unique_images": len(unique_assets),
                "unassigned_images": len(unassigned),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
