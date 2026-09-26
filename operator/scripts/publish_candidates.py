#!/usr/bin/env python3
"""Issue-based publish selection (read-only unless --mark-staged ... --apply).

Selects open issues labelled `review:pass` (+ `candidate`) whose CURRENT skill sha equals the sha recorded in the
review comment marker, that are not protected / critical / publisher-skip, not blocked:* / held / published /
already staged, and turns them into candidate records the existing publish path stages unchanged
(catalog_update.plan_update, publisher derive/rescan/stage). ClawHub-only skills come out with
source_type=clawhub, repo_url = the ClawHub page, no GitHub stars, license MIT-0 (platform evidence) and the
SKILL.md text from the public page.

Output (default): operator/state/artifacts/issue-queue-YYYY-MM-DD.json with the same keys the publisher reads
from candidate-queue-YYYY-MM-DD.json (`passed` in publish order, `eligible`, holds, soft_cap), so the hourly
publisher can switch source by changing one path; the queue-based path keeps working as the fallback.

`--mark-staged 123,456 --apply` adds the publish:staged label after the publisher opened its catalog PR.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scout  # noqa: E402
import issue_flow as F  # noqa: E402
from scout_file import resolve_shas  # noqa: E402

HELD_PREFIXES = ("blocked:", "review:hold", "flag:")


def excluded(issue: dict[str, Any]) -> str | None:
    why = F.untouchable(issue)
    if why:
        return why
    labs = F.issue_labels(issue)
    if F.LABEL_CANDIDATE not in labs:
        return "not_candidate"
    if labs & F.PUBLISHED_LABELS:
        return "published"
    if F.LABEL_STAGED in labs:
        return "already_staged"
    if any(l.startswith(HELD_PREFIXES) for l in labs):
        return "held_label"
    if F.verdict_of(labs) != "review:pass":
        return "verdict_not_pass"
    return None


def reviewed_sha(api: F.IssueRepo, issue: int, identity: str, state: dict[str, Any]) -> str | None:
    """sha from OUR review comment marker (GitHub is the source of truth; state only gives the comment id)."""
    cid = (state.get(str(issue)) or {}).get("comment_id")
    bodies: list[str] = []
    if cid:
        try:
            bodies.append((api.get(f"issues/comments/{int(cid)}") or {}).get("body") or "")
        except scout.GitHubError:
            pass
    if not bodies:
        bodies = [c.get("body") or "" for c in api.list_comments(issue)]
    for b in bodies:
        m = F.parse_review_marker(b)
        if m and m["psk-id"] == identity and m["verdict"] in ("review:pass", "review:needs-ai"):
            return m["sha"]
    return None


def github_record(issue: dict[str, Any], blk: dict[str, Any], live: dict[str, Any], sha: str) -> dict[str, Any]:
    ident = blk["psk-id"]
    rk = F.repo_key_of(ident)
    owner, repo = rk.split("/", 1)
    sub = ident.split("::", 1)[1] if "::" in ident else None
    lic = blk.get("license") or {}
    m = blk.get("metrics") or {}
    return {
        "issue": int(issue["number"]), "identity": ident, "source_type": "github", "owner": owner, "repo": repo,
        "repo_url": f"https://github.com/{rk}", "subpath": sub, "skill_path": blk.get("skill_path"),
        "default_branch": live.get("default_branch") or blk.get("default_branch"),
        "commit_sha": live.get("commit_sha"), "reviewed_sha": sha,
        "stars": int(m.get("stars") or 0), "forks": m.get("forks"), "pushed_at": m.get("pushed_at"),
        "license": lic.get("spdx") or lic.get("label"), "license_tier": "pass",
        "license_evidence": lic.get("evidence") or [], "title": scout.redact_title(int(issue["number"]), issue.get("title") or ""),
        "scan_status": "pass", "selection": "issue_flow:review:pass",
    }


def clawhub_record(issue: dict[str, Any], blk: dict[str, Any], page: dict[str, Any], sha: str,
                   entry: dict[str, Any] | None) -> dict[str, Any]:
    from sources import clawhub as C
    ident = blk["psk-id"]
    owner, _, slug = ident[len("clawhub:"):].lstrip("@").partition("/")
    stats = page.get("stats") or {}
    m = blk.get("metrics") or {}
    return {
        "issue": int(issue["number"]), "identity": ident, "source_type": "clawhub", "owner": owner.lower(),
        "slug": slug.lower(), "repo": None, "repo_url": C.page_url(owner, slug), "source_url": C.page_url(owner, slug),
        "subpath": None, "skill_path": "SKILL.md", "stars": None, "reviewed_sha": sha,
        "version": (entry or {}).get("version") or m.get("version"),
        "skill_name": (entry or {}).get("title"), "skill_description": (entry or {}).get("description"),
        "clawhub_downloads": stats.get("downloads", m.get("downloads")),
        "clawhub_installs": stats.get("installs", stats.get("installsAllTime", m.get("installs"))),
        "clawhub_stars": stats.get("stars"),
        "license": "MIT-0", "license_tier": "pass",
        "license_evidence": ["clawhub_platform_license:MIT-0", f"evidence_url:{C.LICENSE_EVIDENCE_URL}"],
        "skill_md": page.get("skill_md_text"), "title": issue.get("title") or "",
        "scan_status": "pass", "selection": "issue_flow:review:pass",
    }


def select(api: F.IssueRepo, state_dir: Path, *, limit: int = 40, catalog: dict | None = None,
           clawhub: tuple[Any, dict] | None = None, issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    state = F.load_json(state_dir / "review-state.json", {}) or {}
    if issues is None:
        issues = api.list_issues(state="open", labels="review:pass")
    catalog_idx = scout.build_catalog_index(catalog) if catalog is not None else None
    out: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    repos_in_batch: set[str] = set()

    def prio(i: dict[str, Any]) -> tuple:
        b = F.parse_block(i.get("body")) or {}
        m = b.get("metrics") or {}
        return (-int(m.get("stars") or 0), -int(m.get("downloads") or 0) // 10, int(i["number"]))

    for issue in sorted(issues, key=prio):
        if len(out) >= limit:
            break
        n = int(issue["number"])
        why = excluded(issue)
        blk = F.parse_block(issue.get("body"))
        if not why and not blk:
            why = "no_block"
        if not why and blk.get("kind") != "skill":
            why = "not_a_single_skill"
        if why:
            skipped.append({"issue": n, "reason": why})
            continue
        ident = blk["psk-id"]
        rk = F.repo_key_of(ident)
        if rk in repos_in_batch:
            skipped.append({"issue": n, "reason": "one_per_repo_in_batch"})
            continue
        if catalog_idx is not None and ident.startswith("github:") and rk in catalog_idx:
            skipped.append({"issue": n, "reason": "repo_already_in_catalog"})
            continue
        sha = reviewed_sha(api, n, ident, state)
        if not sha:
            skipped.append({"issue": n, "reason": "no_review_marker"})
            continue
        if ident.startswith("clawhub:"):
            if clawhub is None:
                skipped.append({"issue": n, "reason": "clawhub_unavailable"})
                continue
            ch, feed = clawhub
            owner, _, slug = ident[len("clawhub:"):].lstrip("@").partition("/")
            entry = feed.get(f"@{owner}/{slug}".lower())
            if not entry:
                skipped.append({"issue": n, "reason": "not_in_clawhub_feed"})
                continue
            integ = ((entry.get("install") or {}).get("candidates") or [{}])[0].get("integrity")
            cur = "clawhub:" + str(entry.get("version") or "?") + (":" + str(integ).replace("sha256:", "")[:16] if integ else "")
            if cur != sha:
                skipped.append({"issue": n, "reason": "sha_changed", "reviewed": sha, "current": cur})
                continue
            page = ch.page(owner, slug, entry.get("version")) or {}
            if not page.get("ok") or not page.get("skill_md_text") or (page.get("moderation") or {}).get("flagged"):
                skipped.append({"issue": n, "reason": "clawhub_page_not_publishable"})
                continue
            rec = clawhub_record(issue, blk, page, sha, entry)
        else:
            live = {"identity": ident, "source_type": "github", "kind": "skill", "skill_path": blk.get("skill_path"),
                    "default_branch": None}
            st = resolve_shas(api.client, live)
            if st != "ok":
                skipped.append({"issue": n, "reason": f"live_sha_{st}"})
                continue
            if live.get("skill_tree_sha") != sha:
                skipped.append({"issue": n, "reason": "sha_changed", "reviewed": sha, "current": live.get("skill_tree_sha")})
                continue
            rec = github_record(issue, blk, live, sha)
        repos_in_batch.add(rk)
        out.append({**rec, "queue_rank": len(out) + 1})
    return {"passed": out, "skipped": skipped}


def queue_payload(sel: dict[str, Any]) -> dict[str, Any]:
    passed = sel["passed"]
    return {
        "generated_at": F.iso(), "source": "issue_flow", "selector": "operator/scripts/publish_candidates.py",
        "soft_cap": scout.SOFT_CAP, "passed": passed, "passed_count": len(passed), "eligible": passed,
        "eligible_count": len(passed), "license_review": [], "holds_critical_static": [], "holds_other_scan": [],
        "publisher_skip_batch_seed": sorted(F.PUBLISHER_SKIP_ISSUES), "skipped": sel["skipped"],
        "passed_order": "stars desc (ClawHub: downloads); one per repo; repos already in catalog skipped",
        "note": "issue-based selection: open review:pass issues whose live sha equals the reviewed sha",
    }


def mark_staged(api: F.IssueRepo, numbers: list[int], *, apply: bool) -> list[dict[str, Any]]:
    out = []
    for n in numbers:
        issue = api.get_issue(n)
        if F.untouchable(issue) or F.LABEL_CANDIDATE not in F.issue_labels(issue):
            out.append({"issue": n, "action": "refused"})
            continue
        if apply:
            api.add_labels(n, [F.LABEL_STAGED])
        out.append({"issue": n, "action": "label" if apply else "would_label"})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Select review:pass candidate issues for the publisher")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--catalog", type=Path, default=None, help="catalog snapshot (default: live)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--state-dir", type=Path, default=F.STATE_DIR)
    ap.add_argument("--mark-staged", default=None, help="comma-separated issue numbers -> publish:staged")
    ap.add_argument("--apply", action="store_true", help="only with --mark-staged")
    args = ap.parse_args(argv)
    client = scout.GitHubClient(scout.get_gh_token(), throttle=scout.AdaptiveThrottle(4))
    api = F.IssueRepo(client)
    if args.mark_staged:
        nums = [int(x) for x in args.mark_staged.split(",") if x.strip()]
        print(json.dumps(mark_staged(api, nums, apply=args.apply)), flush=True)
        return 0
    from publish_lib import load_catalog
    catalog = load_catalog(args.catalog)
    issues = api.list_issues(state="open", labels="review:pass")
    ch = None
    if any((F.parse_block(i.get("body")) or {}).get("psk-id", "").startswith("clawhub:") for i in issues):
        from review import load_clawhub
        c, feed, cache = load_clawhub(args.state_dir)
        ch = (c, feed)
    sel = select(api, args.state_dir, limit=args.limit, catalog=catalog, clawhub=ch, issues=issues)
    if ch is not None:
        cache.save()
    payload = queue_payload(sel)
    path = args.out or scout.ART / f"issue-queue-{scout.now_dhaka().date().isoformat()}.json"
    F.save_json(path, payload)
    print(json.dumps({"passed": len(sel["passed"]), "skipped": len(sel["skipped"]),
                      "issues": [p["issue"] for p in sel["passed"]]}), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
