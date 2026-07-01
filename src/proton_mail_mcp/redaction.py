from __future__ import annotations

import re
from typing import Any

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)(Authentication|Authorization):\s*([^\r\n]+)"),
    re.compile(r"(?i)(PROTON_BRIDGE_PASSWORD|SIMPLELOGIN_API_KEY)=([^\r\n]+)"),
]


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
    return redacted


def redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        if re.search(r"(?i)(api[_-]?key|token|password|secret|authorization|authentication)", key):
            output[key] = "<redacted>"
        elif isinstance(item, str):
            output[key] = redact_text(item)
        elif isinstance(item, dict):
            output[key] = redact_mapping(item)
        elif isinstance(item, list):
            output[key] = [
                redact_mapping(x) if isinstance(x, dict) else redact_text(x) if isinstance(x, str) else x for x in item
            ]
        else:
            output[key] = item
    return output
