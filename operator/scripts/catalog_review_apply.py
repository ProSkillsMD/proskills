#!/usr/bin/env python3
"""Patch existing catalog rows with catalog-review results (by id/slug only).

Never adds/removes rows. Never changes id/slug/repo_url/category/identity fields.
Only updates: reviewed, scores, verified_at, review_version, review_evidence.
Preserves JSON formatting via publish_lib.catalog_json_dumps (round-trip stable).
Refuses if catalog total would change.
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import emit, exit_fail, exit_ok, resolve_dry_run, add_dry_run_apply_flags  # noqa: E402
from publish_lib import catalog_json_dumps  # noqa: E402

REVIEW_VERSION = "catalog-review/1"
PATCH_KEYS = ("reviewed", "scores", "verified_at", "review_version", "review_evidence")
IDENTITY_KEYS = ("id", "slug", "repo_url", "category", "skill_path", "source_type")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_evidence(row: dict[str, Any]) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "verdict": row.get("verdict"),
        "weighted": row.get("weighted"),
        "checks": {
            k: {"status": (v or {}).get("status"), "detail": (v or {}).get("detail")}
            for k, v in (row.get("checks") or {}).items()
        },
        "judged_by": row.get("judged_by"),
        "reasons": list(row.get("reasons") or [])[:5],
    }
    if row.get("sha"):
        ev["sha"] = row["sha"]
    if row.get("arithmetic_mean") is not None:
        ev["arithmetic_mean"] = row["arithmetic_mean"]
    if row.get("critical"):
        ev["critical"] = True
    return ev


def plan_patches(catalog: dict[str, Any], results: list[dict[str, Any]], *,
                 verified_at: str, review_version: str = REVIEW_VERSION) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (staged_catalog, patch_log). Mutates a deep copy only."""
    staged = deepcopy(catalog)
    skills = staged.get("skills")
    if not isinstance(skills, list):
        raise ValueError("catalog.skills must be a list")
    original_total = len(skills)
    field_total = staged.get("total")
    if field_total is not None and int(field_total) != original_total:
        raise ValueError(f"catalog.total {field_total} != len(skills) {original_total}")

    by_id: dict[str, int] = {}
    by_slug: dict[str, int] = {}
    for i, s in enumerate(skills):
        if not isinstance(s, dict):
            continue
        if s.get("id"):
            by_id[str(s["id"])] = i
        if s.get("slug"):
            by_slug[str(s["slug"])] = i

    patch_log: list[dict[str, Any]] = []
    for row in results:
        sid = str(row.get("id") or "")
        slug = str(row.get("slug") or "")
        idx = by_id.get(sid)
        if idx is None and slug:
            idx = by_slug.get(slug)
        if idx is None:
            raise ValueError(f"skill not found for id={sid!r} slug={slug!r}")
        skill = skills[idx]
        # identity guard
        if sid and skill.get("id") and str(skill["id"]) != sid:
            raise ValueError(f"id mismatch at index {idx}: catalog={skill.get('id')} result={sid}")
        if slug and skill.get("slug") and str(skill["slug"]) != slug:
            # allow id match with different slug only if id matched
            if sid and str(skill.get("id")) == sid:
                pass
            else:
                raise ValueError(f"slug mismatch at index {idx}")

        before = {k: deepcopy(skill.get(k)) for k in PATCH_KEYS}
        reviewed = bool(row.get("reviewed"))
        skill["reviewed"] = reviewed
        if row.get("scores") is not None:
            skill["scores"] = deepcopy(row["scores"])
        # Failures stay reviewed=false; still record evidence + version; verified_at only on pass
        skill["review_version"] = review_version
        skill["review_evidence"] = build_evidence(row)
        if reviewed:
            skill["verified_at"] = verified_at
        # Leave verified_at as-is (usually "") on fail — do not invent a verification date
        after = {k: deepcopy(skill.get(k)) for k in PATCH_KEYS}
        patch_log.append({
            "id": skill.get("id"),
            "slug": skill.get("slug"),
            "index": idx,
            "reviewed": reviewed,
            "before": before,
            "after": after,
        })

    if len(staged["skills"]) != original_total:
        raise ValueError("refusing: skill count changed")
    if staged.get("total") is not None and int(staged["total"]) != original_total:
        raise ValueError("refusing: total field inconsistent after patch")
    return staged, patch_log


def assert_apply_invariants(before: dict[str, Any], after: dict[str, Any],
                            patched_ids: set[str]) -> None:
    """Raise if apply violated invariants."""
    b_skills = before.get("skills") or []
    a_skills = after.get("skills") or []
    if len(b_skills) != len(a_skills):
        raise AssertionError(f"row count changed {len(b_skills)} -> {len(a_skills)}")
    if before.get("total") != after.get("total"):
        raise AssertionError("total field changed")
    for i, (b, a) in enumerate(zip(b_skills, a_skills)):
        bid = str(b.get("id") or b.get("slug") or i)
        for k in IDENTITY_KEYS:
            if b.get(k) != a.get(k):
                raise AssertionError(f"identity field {k} changed on {bid}")
        if bid not in patched_ids and str(b.get("slug") or "") not in patched_ids:
            if b != a:
                raise AssertionError(f"non-target row changed: {bid}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply catalog-review results to skills-catalog.json")
    add_dry_run_apply_flags(ap)
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True, help="review results JSON from catalog_review.py apply")
    ap.add_argument("--out", type=Path, required=True, help="Staged catalog output path")
    ap.add_argument("--delta-out", type=Path, default=None, help="Optional patch log JSON")
    args = ap.parse_args()
    dry_run = resolve_dry_run(args)

    catalog = load_json(args.catalog)
    payload = load_json(args.results)
    results = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(results, list):
        exit_fail("results must be a list")
        return
    verified_at = (payload.get("verified_at") if isinstance(payload, dict) else None) or ""
    if not verified_at:
        exit_fail("verified_at missing in results (YYYY-MM-DD Dhaka)")
        return
    review_version = (payload.get("review_version") if isinstance(payload, dict) else None) or REVIEW_VERSION

    try:
        staged, patch_log = plan_patches(catalog, results, verified_at=verified_at, review_version=review_version)
        patched_ids = {str(p["id"]) for p in patch_log} | {str(p["slug"]) for p in patch_log}
        assert_apply_invariants(catalog, staged, patched_ids)
    except Exception as exc:
        exit_fail(str(exc))
        return

    text = catalog_json_dumps(staged) + "\n"
    if dry_run:
        emit(stage="catalog_review_apply", status="ok", dry_run=True,
             message=f"dry-run: would patch {len(patch_log)} rows; total stays {len(staged['skills'])}")
        if args.delta_out:
            args.delta_out.parent.mkdir(parents=True, exist_ok=True)
            args.delta_out.write_text(json.dumps({"patches": patch_log}, indent=2) + "\n", encoding="utf-8")
        exit_ok()
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    if args.delta_out:
        args.delta_out.parent.mkdir(parents=True, exist_ok=True)
        args.delta_out.write_text(json.dumps({"patches": patch_log}, indent=2) + "\n", encoding="utf-8")
    emit(stage="catalog_review_apply", status="ok", dry_run=False,
         message=f"patched {len(patch_log)} rows -> {args.out}")
    exit_ok()


if __name__ == "__main__":
    main()
