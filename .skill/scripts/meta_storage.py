"""
Hidden meta storage in PlanFix task description.

PlanFix has no built-in place for "integration state per task". This module
stores arbitrary JSON in the description, base64-encoded, between markers
that PlanFix doesn't strip.

⚠️ HTML comments <!-- --> ARE STRIPPED by PlanFix. Don't use them.

Usage:
    meta = extract_meta(task["description"])
    if not meta:
        meta = {"meeting_id": "12345", "rsvp_users": []}
    meta["last_updated"] = "2026-05-23"
    new_desc = inject_meta(task["description"], meta)
    pf.update_task(task_id, {"description": new_desc})
"""
import base64
import json
import re
from typing import Optional

META_BEGIN = "[INTEGRATION_META_v1]"
META_END = "[/INTEGRATION_META_v1]"
META_RE = re.compile(
    r"\[INTEGRATION_META_v1\]([^\[]+)\[/INTEGRATION_META_v1\]",
    re.DOTALL,
)


def extract_meta(description: Optional[str]) -> Optional[dict]:
    """Read meta from task description. Returns None if absent or invalid."""
    if not description or META_BEGIN not in description:
        return None
    m = META_RE.search(description)
    if not m:
        return None
    try:
        return json.loads(base64.b64decode(m.group(1).strip()).decode("utf-8"))
    except Exception:
        return None


def inject_meta(description: Optional[str], meta: dict) -> str:
    """Write meta into description. Replaces existing or appends at end."""
    blob = base64.b64encode(
        json.dumps(meta, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    marker = f"{META_BEGIN}{blob}{META_END}"
    if description and META_BEGIN in description:
        return META_RE.sub(marker, description)
    if description:
        return f"{description}\n\n{marker}"
    return marker


def strip_meta(description: Optional[str]) -> str:
    """Remove meta marker — for display or final cleanup."""
    if not description or META_BEGIN not in description:
        return description or ""
    return META_RE.sub("", description).strip()


def make_marker_pattern(name: str) -> tuple[str, str, re.Pattern]:
    """If you want multiple separate meta blocks, use distinct names."""
    begin = f"[{name}]"
    end = f"[/{name}]"
    pattern = re.compile(
        re.escape(begin) + r"([^\[]+)" + re.escape(end),
        re.DOTALL,
    )
    return begin, end, pattern
