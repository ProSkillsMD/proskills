"""Deterministic content policy (stdlib only; no AI).

Owner ruling: ProSkills never lists adult / NSFW, piracy, or illegal-content skills. Candidates whose name, description, issue
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
LABEL_DESC = "Adult/NSFW, piracy or illegal content: never published (owner content policy)"

# Strong terms: one hit is enough.
_STRONG = re.compile(
    r"\b(?:nsfw|porn\w*|hentai|erotic\w*|nudes?|nudity|naked|xxx|onlyfans|fetish\w*|lewd|"
    r"camgirls?|sexting|sex\s*(?:bot|chat|roleplay|toy)s?|explicit\s+sexual|sexual\s+(?:content|roleplay|images?))\b",
    re.I)
# Piracy: cracking software/licences, warez, DRM/paywall circumvention, ripping copyrighted streams.
_PIRACY = re.compile(
    r"\b(?:warez|keygens?|torrent\w*|pirat(?:e|ed|es|ing)\s+(?:software|movies?|games?|content|streams?|books?)|"
    r"(?:software|licen[cs]e|serial|game|app)\s+crack(?:s|ing|er)?|crack(?:ed|ing)?\s+(?:software|licen[cs]es?|games?|apps?)|"
    r"(?:drm|paywall)[\s-]*(?:removal|remover|bypass\w*|strip\w*|circumvent\w*|crack\w*)|"
    r"(?:remove|bypass|strip|circumvent|crack)\w*\s+(?:the\s+)?(?:drm|paywalls?)|"
    r"rip(?:ping)?\s+(?:netflix|spotify|disney\+?|hulu|copyrighted|drm)\w*)\b",
    re.I)
# Illegal: fraud/carding, building malware, credential theft, drug buying, doxxing, account takeover.
_ILLEGAL = re.compile(
    r"\b(?:carding|cc\s+dumps|credit\s+card\s+(?:dumps|fraud|generator)|commit\w*\s+fraud|"
    r"(?:build\w*|creat\w*|generat\w*|writ(?:e|ing)|develop\w*)\s+(?:a\s+|an\s+)?(?:undetectable\s+)?(?:malware|ransomware|keyloggers?|infostealers?|stealers?)|"
    r"ransomware\s+(?:builder|generator|kit)|(?:password|credential|cookie)\s+stealers?|"
    r"steal\w*\s+(?:passwords|credentials|cookies|session\s+tokens)|"
    r"(?:buy|order|purchas)\w*\s+(?:illegal\s+)?drugs|darknet\s+market\w*|"
    r"doxx?(?:ing|ed)?|account\s+takeover\s+(?:tool|bot|kit)s?)\b",
    re.I)
# Allow-listed phrases that mention NSFW only as something to detect / filter / avoid.
_BENIGN = re.compile(
    r"\b(?:nsfw|nudity|sexual\s+content)[\s-]*(?:detection|detector|filter\w*|classifier|moderation|check)\b"
    r"|\b(?:detect\w*|filter\w*|block\w*|avoid\w*|flag\w*|moderat\w*)\s+(?:of\s+)?(?:nsfw|nudity|explicit\s+sexual|sexual\s+content)\b"
    r"|\b(?:no|non)[\s-]nsfw\b"
    r"|\b(?:detect\w*|prevent\w*|protect\w*|defen[cd]\w*|analy[sz]\w*|against|block\w*)\s+(?:\w+\s+){0,2}?"
    r"(?:fraud|carding|doxx?ing|account\s+takeover|credential\s+theft|(?:password|credential|cookie)\s+stealers?|ransomware|malware)\b",
    re.I)


def matches(*texts: Any) -> list[str]:
    """Sorted unique policy terms found in the given texts (after removing benign moderation phrases)."""
    hits: set[str] = set()
    for t in texts:
        if not t:
            continue
        s = _BENIGN.sub(" ", str(t))
        for rx in (_STRONG, _PIRACY, _ILLEGAL):
            hits.update(m.group(0).lower() for m in rx.finditer(s))
    return sorted(hits)


def violates(*texts: Any) -> bool:
    return bool(matches(*texts))


def candidate_texts(issue: dict[str, Any] | None, blk: dict[str, Any] | None, extra: Iterable[Any] = ()) -> list[Any]:
    issue, blk = issue or {}, blk or {}
    return [issue.get("title"), blk.get("psk-id"), blk.get("name"), blk.get("description"),
            blk.get("skill_name"), blk.get("skill_description"), *extra]
