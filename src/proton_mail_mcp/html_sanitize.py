"""Best-effort, dependency-free sanitization of outbound HTML.

The watcher and mail tools can send HTML the agent composed or quoted from a received (and therefore
attacker-influenced) message. Before that HTML leaves the machine we strip the constructs that turn
passive markup into active content: script/style blocks, framing and embedding tags, inline event
handlers, and ``javascript:``/``vbscript:``/``data:`` URIs.

This is deliberately conservative and has no dependencies. It is defense-in-depth, not a formal
allowlist sanitizer — callers that fully trust their HTML can opt out, and anyone needing hard
guarantees against hostile input should layer a dedicated sanitizer (e.g. nh3) on top.
"""

from __future__ import annotations

import re

# Elements whose entire contents are removed (not just the tag).
_BLOCK_ELEMENTS = ("script", "style", "iframe", "object", "embed", "applet", "frame", "frameset", "form")
# Standalone/framing tags that are simply dropped.
_DROP_TAGS = ("link", "meta", "base")

_BLOCK_RE = re.compile(
    r"<\s*(" + "|".join(_BLOCK_ELEMENTS) + r")\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Unclosed block elements (e.g. a lone <script src=...>) — drop the open tag too.
_OPEN_BLOCK_RE = re.compile(r"<\s*(" + "|".join(_BLOCK_ELEMENTS + _DROP_TAGS) + r")\b[^>]*/?>", re.IGNORECASE)
_CLOSE_BLOCK_RE = re.compile(r"<\s*/\s*(" + "|".join(_BLOCK_ELEMENTS) + r")\s*>", re.IGNORECASE)
# Inline event handlers: on...="..."/'...'/unquoted.
_EVENT_ATTR_RE = re.compile(r"""\son[a-zA-Z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
# Dangerous URI schemes inside href/src/action/etc. (with optional whitespace/entities before the colon).
_DANGEROUS_URI_RE = re.compile(
    r"""(\b(?:href|src|action|formaction|background|xlink:href)\s*=\s*)("|')?\s*(?:javascript|vbscript|data)\s*:[^"'>\s]*""",
    re.IGNORECASE,
)
# CSS expression()/javascript: inside style attributes or blocks.
_CSS_ACTIVE_RE = re.compile(r"(expression\s*\(|javascript\s*:)", re.IGNORECASE)


def sanitize_html(html: str) -> tuple[str, bool]:
    """Return ``(clean_html, active_content_found)``.

    ``active_content_found`` is True when anything was stripped, so callers can surface that a message
    was altered. Passing already-clean HTML returns it unchanged with ``False``.
    """
    if not html:
        return html, False
    cleaned = html
    active = False

    def _drop(pattern: re.Pattern[str], replacement: str, text: str) -> str:
        nonlocal active
        new = pattern.sub(replacement, text)
        if new != text:
            active = True
        return new

    cleaned = _drop(_BLOCK_RE, "", cleaned)
    cleaned = _drop(_OPEN_BLOCK_RE, "", cleaned)
    cleaned = _drop(_CLOSE_BLOCK_RE, "", cleaned)
    cleaned = _drop(_EVENT_ATTR_RE, "", cleaned)
    cleaned = _drop(_DANGEROUS_URI_RE, lambda m: f"{m.group(1)}{m.group(2) or ''}#", cleaned)
    cleaned = _drop(_CSS_ACTIVE_RE, "", cleaned)
    return cleaned, active
