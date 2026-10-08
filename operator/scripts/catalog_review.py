#!/usr/bin/env python3
"""Catalog review stages for already-indexed skills (badge write-back).

Does NOT set reviewed=true on intake; that is catalog_review_apply.py's job.

Stages:
  s1       Deterministic checks on queue items (static scan, license, SKILL.md,
           reachable source). Failures stay listed; reasons recorded.
  prepare  Build agent-judged AI request files (reuse ai_review SYSTEM/WEIGHTS/
           THRESHOLD, <=8k tokens). No API keys.
  apply    Validate judged JSON results + merge with S1 -> review results JSON
           suitable for catalog_review_apply.py.

Badge rule (gating): reviewed=true only if S1 pass AND AI verdict=pass with
Gamora-weighted score >= 6.0. No Drax sandbox claim.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_review  # noqa: E402
from _common import emit, exit_fail, exit_ok  # noqa: E402
from publish_lib import parse_github_source, parse_skill_frontmatter  # noqa: E402
from static_scan import scan_candidate  # noqa: E402

DHAKA = ZoneInfo("Asia/Dhaka")
REVIEW_VERSION = "catalog-review/1"
SCORE_DIMS = tuple(ai_review.WEIGHTS.keys())
WEIGHTS = ai_review.WEIGHTS
THRESHOLD = ai_review.THRESHOLD
SYSTEM = ai_review.SYSTEM
MAX_INPUT_TOKENS = ai_review.MAX_INPUT_TOKENS


def dhaka_today() -> str:
    return datetime.now(DHAKA).strftime("%Y-%m-%d")


def weighted_average(scores: dict[str, float]) -> float:
    """Gamora-weighted average rounded to 1 decimal (catalog badge field)."""
    return round(sum(WEIGHTS[k] * float(scores[k]) for k in WEIGHTS) / sum(WEIGHTS.values()), 1)


def arithmetic_mean(scores: dict[str, float]) -> float:
    return round(sum(float(scores[k]) for k in SCORE_DIMS) / len(SCORE_DIMS), 2)


def gating_pass(*, s1_ok: bool, verdict: str, weighted: float) -> bool:
    """Badge rule: S1 pass + AI verdict pass + weighted >= THRESHOLD."""
    return bool(s1_ok) and verdict == "pass" and float(weighted) >= float(THRESHOLD)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_catalog_for_ids(catalog_path: Path) -> dict[str, dict[str, Any]]:
    data = load_json(catalog_path)
    out: dict[str, dict[str, Any]] = {}
    for s in data.get("skills") or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or s.get("slug")
        if sid:
            out[str(sid)] = s
        slug = s.get("slug")
        if slug and str(slug) not in out:
            out[str(slug)] = s
    return out


def _owner_repo(repo_url: str | None) -> tuple[str, str] | None:
    parsed = parse_github_source(repo_url)
    if parsed and parsed.get("owner") and parsed.get("repo"):
        return str(parsed["owner"]), str(parsed["repo"])
    return None


def check_reachable(repo_url: str | None, source_type: str | None = None) -> tuple[bool, str]:
    if not repo_url:
        return False, "missing repo_url"
    if source_type == "clawhub" or "clawhub.ai" in str(repo_url) or "clawhub.com" in str(repo_url):
        proc = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-L", "-A", "proskills-operator/1.0",
             "--max-time", "20", str(repo_url)],
            capture_output=True, text=True, check=False,
        )
        code = (proc.stdout or "").strip()
        ok = code.startswith("2") or code.startswith("3")
        return ok, f"http {code}"
    pair = _owner_repo(repo_url)
    if not pair:
        proc = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-L", "-A", "proskills-operator/1.0",
             "--max-time", "20", str(repo_url)],
            capture_output=True, text=True, check=False,
        )
        code = (proc.stdout or "").strip()
        return code.startswith("2") or code.startswith("3"), f"http {code}"
    owner, repo = pair
    proc = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}", "--jq", ".full_name"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return True, f"gh {proc.stdout.strip()}"
    return False, (proc.stderr or proc.stdout or "unreachable")[:160]


def fetch_license_spdx(repo_url: str | None, frontmatter_license: str | None,
                       source_type: str | None = None) -> tuple[str, str | None, list[str]]:
    """Return (tier, label, evidence) matching scout.license_tier categories."""
    if source_type == "clawhub" or (repo_url and "clawhub" in str(repo_url)):
        return "pass", "MIT-0", ["clawhub_platform_license"]
    pair = _owner_repo(repo_url)
    spdx = None
    evidence: list[str] = []
    if pair:
        owner, repo = pair
        proc = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}", "--jq", ".license.spdx_id // empty"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if proc.returncode == 0:
            spdx = (proc.stdout or "").strip() or None
    if spdx and spdx not in ("NONE", "NOASSERTION"):
        return "pass", spdx, [f"repo_spdx:{spdx}"]
    if spdx == "NOASSERTION":
        evidence.append("repo_license_noassertion")
    if frontmatter_license:
        evidence.append(f"frontmatter_license:{frontmatter_license[:80]}")
        return "license_review", frontmatter_license, evidence
    if evidence:
        return "license_review", spdx or "unclassified", evidence
    return "reject", None, []


def scan_skill_text(skill_md: str) -> dict[str, Any]:
    tmp = Path(tempfile.mkdtemp(prefix="catalog-review-scan-"))
    try:
        (tmp / "SKILL.md").write_text(skill_md or "", encoding="utf-8")
        return scan_candidate(
            tmp,
            {"max_files_per_candidate": 4, "max_total_bytes_per_candidate": 400_000},
            project_root=tmp,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_s1_on_skill(skill: dict[str, Any], *, offline: bool = False) -> dict[str, Any]:
    """Deterministic checks. offline=True skips network (reachable/license gh)."""
    sid = str(skill.get("id") or skill.get("slug") or "")
    slug = str(skill.get("slug") or sid)
    skill_md = skill.get("skill_md") or ""
    checks: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    critical = False

    if skill_md and str(skill_md).strip():
        checks["skill_md_present"] = {"status": "pass", "detail": f"{len(skill_md)} chars"}
    else:
        checks["skill_md_present"] = {"status": "fail", "detail": "missing skill_md in catalog row"}
        reasons.append("missing SKILL.md content")

    fm = parse_skill_frontmatter(skill_md) if skill_md else {}
    name = (fm.get("name") or "").strip() or str(skill.get("name") or "").strip()
    desc = (fm.get("description") or "").strip() or str(skill.get("description") or "").strip()
    if name and desc:
        checks["frontmatter"] = {"status": "pass", "detail": f"name={name[:40]}"}
    else:
        missing = " and ".join(x for x, v in (("name", name), ("description", desc)) if not v)
        checks["frontmatter"] = {"status": "fail", "detail": f"missing {missing}"}
        reasons.append(f"frontmatter missing {missing}")

    if skill_md:
        sr = scan_skill_text(skill_md)
        sev = sr.get("max_severity") or "none"
        counts = sr.get("finding_counts") or {}
        findings = sr.get("findings") or []
        has_crit = sev == "critical" or any((f.get("severity") == "critical") for f in findings)
        if has_crit:
            critical = True
            checks["static_scan"] = {
                "status": "fail",
                "detail": f"critical finding; {counts}",
                "max_severity": sev,
            }
            reasons.append("critical static scan finding")
        else:
            checks["static_scan"] = {
                "status": "pass",
                "detail": f"max_severity={sev}; {counts}",
                "max_severity": sev,
            }
    else:
        checks["static_scan"] = {"status": "fail", "detail": "unscannable (no skill_md)"}
        reasons.append("unscannable")

    if offline:
        # Prefer SPDX already on the queue/skill row; else frontmatter / clawhub.
        existing = skill.get("license") if isinstance(skill.get("license"), str) else None
        queue_spdx = skill.get("license_spdx") if isinstance(skill.get("license_spdx"), str) else None
        spdx = queue_spdx or existing
        if spdx and spdx not in ("NONE", "NOASSERTION"):
            tier, label, evidence = "pass", spdx, [f"provided_spdx:{spdx}"]
        elif skill.get("source_type") == "clawhub" or (skill.get("repo_url") and "clawhub" in str(skill.get("repo_url"))):
            tier, label, evidence = "pass", "MIT-0", ["clawhub_platform_license"]
        elif fm.get("license"):
            tier, label, evidence = "license_review", str(fm.get("license")), [f"frontmatter_license:{fm.get('license')}"]
        else:
            tier, label, evidence = "reject", None, ["offline_no_spdx"]
    else:
        tier, label, evidence = fetch_license_spdx(
            skill.get("repo_url"), fm.get("license"), skill.get("source_type"),
        )
    if tier == "pass":
        checks["license"] = {"status": "pass", "detail": label, "tier": tier, "evidence": evidence}
    else:
        checks["license"] = {
            "status": "fail",
            "detail": label or "no SPDX",
            "tier": tier,
            "evidence": evidence,
        }
        reasons.append(f"license:{tier}")

    if offline:
        checks["reachable"] = {"status": "pass", "detail": "skipped offline"}
    else:
        reachable_ok, detail = check_reachable(skill.get("repo_url"), skill.get("source_type"))
        checks["reachable"] = {"status": "pass" if reachable_ok else "fail", "detail": detail}
        if not reachable_ok:
            reasons.append("source unreachable")

    s1_ok = (
        checks["skill_md_present"]["status"] == "pass"
        and checks["frontmatter"]["status"] == "pass"
        and checks["static_scan"]["status"] == "pass"
        and checks["license"]["status"] == "pass"
        and checks["reachable"]["status"] == "pass"
        and not critical
    )
    return {
        "id": sid,
        "slug": slug,
        "s1_ok": s1_ok,
        "critical": critical,
        "checks": checks,
        "reasons": reasons,
        "skill_md": skill_md,
        "sha": None,
        "repo_url": skill.get("repo_url"),
        "github_stars": skill.get("github_stars"),
    }


def cmd_s1(args: argparse.Namespace) -> None:
    queue = load_json(args.queue)
    catalog_idx = load_catalog_for_ids(args.catalog)
    items = queue.get("items") if isinstance(queue, dict) else queue
    if not isinstance(items, list):
        items = []
    results = []
    for it in items:
        sid = str(it.get("id") or it.get("slug") or "")
        skill = catalog_idx.get(sid) or catalog_idx.get(str(it.get("slug") or ""))
        if not skill:
            results.append({
                "id": sid, "slug": it.get("slug"), "s1_ok": False, "critical": False,
                "checks": {}, "reasons": ["not found in catalog"],
            })
            continue
        merged = dict(skill)
        if it.get("license_spdx") and not merged.get("license"):
            merged["license_spdx"] = it["license_spdx"]
        row = run_s1_on_skill(merged, offline=args.offline)
        if not args.keep_md:
            row = {k: v for k, v in row.items() if k != "skill_md"}
            row["skill_md_chars"] = len(skill.get("skill_md") or "")
        results.append(row)
    out = {
        "stage": "s1",
        "review_version": REVIEW_VERSION,
        "generated_at": datetime.now(DHAKA).isoformat(),
        "results": results,
        "pass_count": sum(1 for r in results if r.get("s1_ok")),
        "fail_count": sum(1 for r in results if not r.get("s1_ok")),
        "critical_count": sum(1 for r in results if r.get("critical")),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    emit(stage="catalog_review_s1", status="ok", dry_run=True,
         message=f"s1 pass={out['pass_count']} fail={out['fail_count']} critical={out['critical_count']}")
    exit_ok()


def build_catalog_prompt(skill_id: str, slug: str, sha: str, s1_summary: str, skill_md: str,
                         max_tokens: int = MAX_INPUT_TOKENS) -> tuple[str, int, bool]:
    snippets = ai_review.flagged_snippets(skill_md)
    head = (
        f"{SYSTEM}\n\n"
        f"catalog_id: {skill_id}\nslug: {slug}\nsha: {sha}\n\n"
        f"Deterministic S1 results:\n{s1_summary}\n\n"
        f"Flagged static-scan snippets (JSON):\n{json.dumps(snippets, ensure_ascii=False)}\n\n"
        "Reply with ONE JSON object and nothing else: "
        '{"id": "<catalog id>", "slug": "<slug>", "sha": "<sha as given>", '
        '"verdict": "pass"|"hold", "reasons": [<=5 short strings], '
        '"scores": {"functionality": n, "documentation": n, "security": n, '
        '"maintenance": n, "usefulness": n, "uniqueness": n, "code_quality": n}}.\n\n'
    )
    budget_chars = max(0, max_tokens * ai_review.CHARS_PER_TOKEN - len(head) - 40)
    truncated = len(skill_md) > budget_chars
    body = skill_md[:budget_chars]
    prompt = f"{head}<skill_md>\n{body}\n</skill_md>\n"
    return prompt, ai_review.est_tokens(prompt), truncated


def cmd_prepare(args: argparse.Namespace) -> None:
    s1 = load_json(args.s1)
    catalog_idx = load_catalog_for_ids(args.catalog)
    req_dir = args.requests_dir
    req_dir.mkdir(parents=True, exist_ok=True)
    items = []
    skipped = []
    for r in s1.get("results") or []:
        sid = str(r.get("id") or r.get("slug") or "")
        if not r.get("s1_ok"):
            skipped.append({"id": sid, "reason": "s1_fail", "s1_reasons": r.get("reasons")})
            continue
        skill = catalog_idx.get(sid) or catalog_idx.get(str(r.get("slug") or ""))
        if not skill:
            skipped.append({"id": sid, "reason": "not_in_catalog"})
            continue
        skill_md = skill.get("skill_md") or ""
        if not skill_md:
            skipped.append({"id": sid, "reason": "no_skill_md"})
            continue
        s1_summary = json.dumps(
            {"s1_ok": r.get("s1_ok"), "checks": r.get("checks"), "reasons": r.get("reasons")},
            ensure_ascii=False,
        )
        sha = str(r.get("sha") or "catalog")
        prompt, toks, trunc = build_catalog_prompt(sid, str(r.get("slug") or sid), sha, s1_summary, skill_md)
        req = {
            "id": sid,
            "slug": r.get("slug") or sid,
            "sha": sha,
            "est_input_tokens": toks,
            "skill_md_truncated": trunc,
            "weights": WEIGHTS,
            "threshold": THRESHOLD,
            "prompt": prompt,
        }
        path = req_dir / f"request-{sid}.json"
        path.write_text(json.dumps(req, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        items.append({"id": sid, "slug": r.get("slug"), "path": str(path), "est_input_tokens": toks})
    manifest = {
        "stage": "prepare",
        "review_version": REVIEW_VERSION,
        "weights": WEIGHTS,
        "threshold": THRESHOLD,
        "response_schema": {
            "id": "str", "slug": "str", "sha": "str", "verdict": "pass|hold",
            "reasons": "list[str] (<=5)",
            "scores": {k: "number 0-10" for k in WEIGHTS},
        },
        "items": items,
        "skipped": skipped,
        "instructions": (
            "For each request file, read prompt and reply with one JSON object per schema; "
            "collect as {\"results\": [...]} then run: "
            "catalog_review.py apply --judged FILE --s1 S1 --out results.json"
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    emit(stage="catalog_review_prepare", status="ok", dry_run=True,
         message=f"prepared {len(items)} requests, skipped {len(skipped)}")
    exit_ok()


class JudgedError(ValueError):
    pass


def validate_judged(r: Any) -> dict[str, Any]:
    if not isinstance(r, dict):
        raise JudgedError("result is not an object")
    required = {"id", "verdict", "reasons", "scores"}
    missing = required - set(r)
    if missing:
        raise JudgedError(f"missing keys {sorted(missing)}")
    if r["verdict"] not in ("pass", "hold"):
        raise JudgedError("verdict must be pass|hold")
    if not isinstance(r["reasons"], list) or len(r["reasons"]) > 5:
        raise JudgedError("reasons must be list <=5")
    sc = r["scores"]
    if not isinstance(sc, dict) or set(sc) != set(WEIGHTS):
        raise JudgedError(f"scores must have exactly {sorted(WEIGHTS)}")
    for k, v in sc.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= float(v) <= 10):
            raise JudgedError(f"score {k} out of range")
    return r


def cmd_apply(args: argparse.Namespace) -> None:
    s1 = load_json(args.s1)
    judged_raw = load_json(args.judged)
    judged_list = judged_raw.get("results") if isinstance(judged_raw, dict) else judged_raw
    if not isinstance(judged_list, list):
        exit_fail("judged file must be list or {results: [...]}")
        return
    s1_by_id = {str(r.get("id") or r.get("slug")): r for r in (s1.get("results") or [])}
    judged_by_id: dict[str, dict[str, Any]] = {}
    for raw in judged_list:
        try:
            j = validate_judged(raw)
        except JudgedError as exc:
            exit_fail(f"invalid judged result: {exc}")
            return
        judged_by_id[str(j["id"])] = j

    results = []
    for sid, s1r in s1_by_id.items():
        base = {
            "id": sid,
            "slug": s1r.get("slug") or sid,
            "s1_ok": bool(s1r.get("s1_ok")),
            "critical": bool(s1r.get("critical")),
            "checks": s1r.get("checks") or {},
            "s1_reasons": s1r.get("reasons") or [],
        }
        if not s1r.get("s1_ok"):
            results.append({
                **base,
                "verdict": "fail",
                "reviewed": False,
                "scores": None,
                "weighted": None,
                "reasons": list(s1r.get("reasons") or ["s1_fail"]),
                "judged_by": None,
            })
            continue
        j = judged_by_id.get(sid)
        if not j:
            results.append({
                **base,
                "verdict": "hold",
                "reviewed": False,
                "scores": None,
                "weighted": None,
                "reasons": ["missing AI judgment"],
                "judged_by": None,
            })
            continue
        scores = {k: float(j["scores"][k]) for k in WEIGHTS}
        w = weighted_average(scores)
        w2 = ai_review.weighted(scores)
        passed = gating_pass(s1_ok=True, verdict=str(j["verdict"]), weighted=w)
        final_verdict = "pass" if passed else "hold"
        scores_out = {**scores, "average": w}
        results.append({
            **base,
            "verdict": final_verdict,
            "ai_verdict": j["verdict"],
            "reviewed": passed,
            "scores": scores_out,
            "weighted": w,
            "weighted_raw": w2,
            "arithmetic_mean": arithmetic_mean(scores),
            "reasons": list(j.get("reasons") or [])[:5],
            "sha": j.get("sha") or s1r.get("sha"),
            "judged_by": args.judged_by or "agent",
        })

    out = {
        "stage": "review_results",
        "review_version": REVIEW_VERSION,
        "verified_at": args.verified_at or dhaka_today(),
        "threshold": THRESHOLD,
        "weights": WEIGHTS,
        "results": results,
        "pass_count": sum(1 for r in results if r.get("reviewed")),
        "fail_count": sum(1 for r in results if not r.get("reviewed")),
        "critical_ids": [r["id"] for r in results if r.get("critical")],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    emit(stage="catalog_review_apply_judged", status="ok", dry_run=True,
         message=f"results pass={out['pass_count']} fail={out['fail_count']}")
    exit_ok()


def main() -> None:
    ap = argparse.ArgumentParser(description="Catalog review S1 / prepare / apply-judged")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("s1", help="Run deterministic checks on queue items")
    p1.add_argument("--queue", type=Path, required=True)
    p1.add_argument("--catalog", type=Path, required=True)
    p1.add_argument("--out", type=Path, required=True)
    p1.add_argument("--offline", action="store_true", help="Skip network reachable/license lookups")
    p1.add_argument("--keep-md", action="store_true")
    p1.set_defaults(func=cmd_s1)

    pp = sub.add_parser("prepare", help="Write AI request files for S1-pass items")
    pp.add_argument("--s1", type=Path, required=True)
    pp.add_argument("--catalog", type=Path, required=True)
    pp.add_argument("--requests-dir", type=Path, required=True)
    pp.add_argument("--out", type=Path, required=True, help="Manifest JSON")
    pp.set_defaults(func=cmd_prepare)

    pa = sub.add_parser("apply", help="Merge judged AI results with S1 into review results")
    pa.add_argument("--judged", type=Path, required=True)
    pa.add_argument("--s1", type=Path, required=True)
    pa.add_argument("--out", type=Path, required=True)
    pa.add_argument("--judged-by", default="agent")
    pa.add_argument("--verified-at", default=None, help="YYYY-MM-DD (Dhaka); default today")
    pa.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
