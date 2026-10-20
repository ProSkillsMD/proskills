"""Deterministic content policy (stdlib only; no AI).

Owner ruling: ProSkills never lists adult / NSFW skills. Candidates whose name, description, issue
title or SKILL.md text match the patterns below get the ``hold:content-policy`` label in review and are
never selected by publish. Patterns are word-bounded so 'sexagesimal', 'Sextant', 'Essex' etc. do not
match; health/compliance mentions ('sexual health', 'sexual orientation') are only hits when combined
with a strong term, so they are not held on their own.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

LABEL = "hold:content-policy"
LABEL_COLOR = "b60205"
LABEL_DESC = "Adult/NSFW content: never published (owner content policy)"

# Strong terms: one hit is enough.
_STRONG = re.compile(
    r"\b(?:nsfw|porn\w*|hentai|erotic\w*|nudes?|nudity|naked|xxx|onlyfans|fetish\w*|lewd|"
    r"camgirls?|sexting|sex\s*(?:bot|chat|roleplay|toy)s?|explicit\s+sexual|sexual\s+(?:content|roleplay|images?))\b",
    re.I)
# Allow-listed phrases that mention NSFW only as something to detect / filter / avoid.
_BENIGN = re.compile(
    r"\b(?:nsfw|nudity|sexual\s+content)[\s-]*(?:detection|detector|filter\w*|classifier|moderation|check)\b"
    r"|\b(?:detect\w*|filter\w*|block\w*|avoid\w*|flag\w*|moderat\w*)\s+(?:of\s+)?(?:nsfw|nudity|explicit\s+sexual|sexual\s+content)\b"
    r"|\b(?:no|non)[\s-]nsfw\b",
    re.I)


def matches(*texts: Any) -> list[str]:
    """Sorted unique policy terms found in the given texts (after removing benign moderation phrases)."""
    hits: set[str] = set()
    for t in texts:
        if not t:
            continue
        s = _BENIGN.sub(" ", str(t))
        hits.update(m.group(0).lower() for m in _STRONG.finditer(s))
    return sorted(hits)


def violates(*texts: Any) -> bool:
    return bool(matches(*texts))


def candidate_texts(issue: dict[str, Any] | None, blk: dict[str, Any] | None, extra: Iterable[Any] = ()) -> list[Any]:
    issue, blk = issue or {}, blk or {}
    return [issue.get("title"), blk.get("psk-id"), blk.get("name"), blk.get("description"),
            blk.get("skill_name"), blk.get("skill_description"), *extra]
