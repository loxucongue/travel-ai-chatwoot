"""JSON protocol helpers."""
import hashlib,json

def _request_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_object(content: str) -> dict:
    value = content.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("json_object_required")
    return parsed


def combine_digests(*digests: str) -> str:
    return hashlib.sha256("|".join(digests).encode()).hexdigest()
