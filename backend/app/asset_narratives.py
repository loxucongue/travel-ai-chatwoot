"""Reviewed, non-instructional narrative fields attached to material assets."""
from __future__ import annotations


NARRATIVE_FIELDS = (
    "what_it_shows",
    "feature_points",
    "customer_value",
    "recommended_caption",
    "avoid_claims",
)


def normalized_asset_narrative(source: dict | None) -> dict:
    source = source or {}
    return {
        "what_it_shows": str(source.get("what_it_shows") or "").strip(),
        "feature_points": list(dict.fromkeys(
            str(item).strip() for item in (source.get("feature_points") or [])
            if str(item).strip()
        )),
        "customer_value": str(source.get("customer_value") or "").strip(),
        "recommended_caption": str(source.get("recommended_caption") or "").strip(),
        "avoid_claims": list(dict.fromkeys(
            str(item).strip() for item in (source.get("avoid_claims") or [])
            if str(item).strip()
        )),
    }


def asset_prompt_item(material: dict | None, key: str) -> dict:
    material = material or {}
    narrative = normalized_asset_narrative(material)
    return {
        "id": key,
        "name": str(material.get("name") or material.get("display_name") or ""),
        "topic": str(material.get("topic") or material.get("usage") or ""),
        **narrative,
    }


def selected_asset_claims(context: dict, asset_ids: list[str]) -> list[dict]:
    by_key = {
        str(item.get("key") or item.get("id") or ""): item
        for item in context.get("available_materials") or []
    }
    return [asset_prompt_item(by_key.get(key), key) for key in asset_ids if key in by_key]
