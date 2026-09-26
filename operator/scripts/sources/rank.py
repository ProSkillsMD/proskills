"""Ranking: evergreen lane (stars + recency) and rising lane (star velocity from dated readings)."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .base import iso, parse_iso

DEFAULT_RANKING = {
    "evergreen_min_stars": 100, "evergreen_max_idle_days": 365, "rising_min_hours_between_readings": 20,
    "rising_min_stars_per_day": 5, "rising_min_growth_pct_per_day": 3, "rising_min_stars": 20,
    "multi_source_bonus": 0.3, "readings_kept": 14,
}


def record_reading(readings: dict[str, list], key: str, stars: int, now: datetime, keep: int = 14) -> list:
    """Store one dated star reading per UTC day for `key` (same-day readings are replaced)."""
    day = now.date().isoformat()
    lst = [r for r in readings.get(key) or [] if isinstance(r, list) and len(r) == 2 and not str(r[0]).startswith(day)]
    lst.append([iso(now), int(stars)])
    lst.sort(key=lambda r: r[0])
    readings[key] = lst[-keep:]
    return readings[key]


def rising_metrics(lst: list, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Velocity between the newest reading and the newest reading >= min hours older. None if < 2 readings."""
    if not lst or len(lst) < 2:
        return None
    newest_at, newest = parse_iso(lst[-1][0]), int(lst[-1][1])
    min_h = float(cfg.get("rising_min_hours_between_readings", 20))
    for at_s, stars in reversed(lst[:-1]):
        at = parse_iso(at_s)
        if not at or not newest_at:
            continue
        hours = (newest_at - at).total_seconds() / 3600.0
        if hours >= min_h:
            days = hours / 24.0
            per_day = (newest - int(stars)) / days
            growth = (100.0 * per_day / int(stars)) if int(stars) > 0 else (100.0 if per_day > 0 else 0.0)
            return {"from": at_s, "to": lst[-1][0], "days": round(days, 2), "stars_from": int(stars),
                    "stars_to": newest, "stars_per_day": round(per_day, 2), "growth_pct_per_day": round(growth, 2)}
    return None


def recency_bonus(pushed_at: str | None, now: datetime) -> float:
    dt = parse_iso(pushed_at)
    if not dt:
        return 0.0
    days = (now - dt).total_seconds() / 86400.0
    if days <= 30:
        return 0.5
    if days <= 90:
        return 0.25
    if days <= 365:
        return 0.0
    return -0.5


def assign_lane(rec: dict[str, Any], readings_for_repo: list | None, cfg: dict[str, Any], now: datetime) -> None:
    """Set rec['lane'], rec['score'], rec['rising'] (in place)."""
    c = {**DEFAULT_RANKING, **(cfg or {})}
    stars = int(rec.get("stars") or 0)
    n_src = len({s.get("source") for s in rec.get("sources") or []})
    bonus = float(c["multi_source_bonus"]) * max(0, n_src - 1)
    rec["multi_source_count"] = n_src
    rising = rising_metrics(readings_for_repo or [], c)
    rec["rising"] = rising
    if rising and stars >= int(c["rising_min_stars"]) and (
            rising["stars_per_day"] >= float(c["rising_min_stars_per_day"])
            or rising["growth_pct_per_day"] >= float(c["rising_min_growth_pct_per_day"])):
        rec["lane"] = "rising"
        rec["score"] = round(math.log10(max(0.0, rising["stars_per_day"]) + 1) * 2 + bonus
                             + recency_bonus(rec.get("pushed_at"), now), 4)
        return
    idle_ok = True
    dt = parse_iso(rec.get("pushed_at"))
    if dt and (now - dt).days > int(c["evergreen_max_idle_days"]):
        idle_ok = False
    if stars >= int(c["evergreen_min_stars"]) and idle_ok:
        rec["lane"] = "evergreen"
    else:
        rec["lane"] = "watch"
    rec["rising_status"] = "insufficient_readings" if rising is None else "below_threshold"
    rec["score"] = round(math.log10(stars + 1) + recency_bonus(rec.get("pushed_at"), now) + bonus, 4)
