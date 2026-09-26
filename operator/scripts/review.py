#!/usr/bin/env python3
"""Deterministic review of candidate issues (no AI). Default --dry-run; --apply writes.

Selects open issues labelled `candidate` that carry a `proskills:candidate v1` block and either have no
review:* verdict label, or whose block sha differs from the last reviewed sha (<= --limit 50 per run).

Checks (fixed order): source exists, SKILL.md at the recorded path, frontmatter name + description, license
tier, static_scan severity (operator/scripts/static_scan.py; text only, never executed), catalog duplicate by
identity, ClawHub platform moderation flags, skill sha.

Exactly one review comment per issue, keyed by `<!-- proskills:review v1 psk-id=... sha=... verdict=... -->`
(edited in place, never a second one; nothing is written when verdict and sha are unchanged). Exactly one
verdict label: review:pass | review:license-review | review:hold-critical | review:needs-ai | review:reject
(+ reject:no-skill-md | reject:no-license | reject:not-found | reject:duplicate) | review:large-collection.
Auto-close (as not planned, no extra comment) only reject:duplicate and reject:not-found, the latter only when
not-found was seen on two runs >= 24 h apart. Protected / critical / publisher-skip / blocked issues are
never touched.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scout  # noqa: E402
import issue_flow as F  # noqa: E402
from publish_lib import load_catalog, parse_skill_frontmatter  # noqa: E402

STATE_NAME = "review-state.json"
NOT_FOUND_CONFIRM = timedelta(hours=24)
CHECKS = ("Source exists", "SKILL.md at path", "Frontmatter name + description", "License",
          "Static scan", "Catalog duplicate", "ClawHub platform flags", "Skill sha")
CLOSE_REASONS = {"reject:duplicate", "reject:not-found"}


class Review:
    def __init__(self, identity: str, kind: str):
        self.identity = identity
        self.kind = kind
        self.checks: dict[str, tuple[str, str]] = {c: ("n/a", "") for c in CHECKS}
        self.findings: list[str] = []
        self.sha: str | None = None
        self.outcome: str | None = None   # transient | not_found | None
        self.verdict: str | None = None
        self.reject: str | None = None
        self.critical = False
        self.inputs: dict[str, Any] = {}  # material for ai_review (SKILL.md text, flagged snippets)

    def check(self, name: str, status: str, detail: str = "") -> None:
        self.checks[name] = (status, detail)


# --------------------------------------------------------------------------- checks

def check_github(client: scout.GitHubClient, blk: dict[str, Any], catalog_idx: dict | None, rv: Review,
                 number: int) -> None:
    ident = blk["psk-id"]
    rk = F.repo_key_of(ident)
    owner, repo = rk.split("/", 1)
    try:
        meta = client.rest(f"repos/{owner}/{repo}") or {}
    except scout.NotFound:
        rv.outcome = "not_found"
        rv.check("Source exists", "fail", f"https://github.com/{rk} returns 404/410")
        return
    except scout.GitHubError as e:
        rv.outcome = "transient"
        rv.findings.append(f"GitHub API error: {type(e).__name__}")
        return
    full = str(meta.get("full_name") or rk)
    if full.lower() != rk:
        rv.findings.append(f"repository moved to {F.safe_text(full, 100)}")
        owner, repo = full.split("/", 1)
    branch = meta.get("default_branch") or blk.get("default_branch") or "main"
    arch = " (archived)" if meta.get("archived") else ""
    rv.check("Source exists", "warn" if arch else "pass", f"https://github.com/{full.lower()} (branch `{F.safe_text(branch, 60)}`){arch}")
    if meta.get("archived"):
        rv.findings.append("repository is archived")
    try:
        tree = client.rest(f"repos/{owner}/{repo}/git/trees/{quote(branch, safe='')}?recursive=1") or {}
    except (scout.NotFound, scout.Unprocessable):
        tree = {}
    except scout.GitHubError as e:
        rv.outcome = "transient"
        rv.findings.append(f"GitHub tree error: {type(e).__name__}")
        return
    entries = tree.get("tree") or []
    skills = scout.discover_skills(entries)
    lic_files = scout.license_files(entries)
    dup_idx_total = len(skills)
    def root_tree_sha() -> str | None:
        # git/trees/<ref> echoes the COMMIT sha; the root tree sha (what scout_file records) is commit.tree.sha
        try:
            com = client.rest(f"repos/{owner}/{repo}/commits/{quote(branch, safe='')}") or {}
        except scout.GitHubError:
            return None
        return ((com.get("commit") or {}).get("tree") or {}).get("sha")

    if blk.get("kind") == "large-collection":
        rv.sha = root_tree_sha()
        rv.check("SKILL.md at path", "pass" if skills else "fail", f"{len(skills)} SKILL.md files in the repository")
        rv.check("Skill sha", "pass", f"`{(rv.sha or '')[:12]}` (repository root tree)")
        if catalog_idx is not None and any((catalog_idx.get(k) or {}).get("whole_repo") for k in {rk, full.lower()}):
            rv.check("Catalog duplicate", "fail", "repository already listed")
            rv.reject = "reject:duplicate"
        else:
            rv.check("Catalog duplicate", "pass", "repository not listed")
        if catalog_idx is None:
            rv.outcome = "transient"
        rv.inputs = {"skills_total": len(skills)}
        return
    path = str(blk.get("skill_path") or "SKILL.md")
    hit = next((s for s in skills if s["skill_path"] == path), None) or \
        next((s for s in skills if s["skill_path"].lower() == path.lower()), None)
    folder = path.rsplit("/", 1)[0] if "/" in path else None
    text = client.raw(owner, repo, branch, hit["skill_path"] if hit else path) if (hit or tree.get("truncated")) else None
    if text is None:
        rv.check("SKILL.md at path", "fail", f"`{F.safe_text(path, 120)}` not found on `{F.safe_text(branch, 60)}`")
        rv.reject = "reject:no-skill-md"
        return
    if hit:
        path = hit["skill_path"]
    rv.check("SKILL.md at path", "pass", f"`{F.safe_text(path, 120)}` ({len(text):,} chars)")
    dir_sha = {e["path"]: e.get("sha") for e in entries if e.get("type") == "tree"}
    rv.sha = dir_sha.get(folder) if folder else root_tree_sha()
    old = F.block_sha(blk)
    rv.check("Skill sha", "pass" if rv.sha == old else "warn",
             f"`{(rv.sha or 'unknown')[:12]}`" + ("" if rv.sha == old else f" (block had `{(old or 'none')[:12]}`)"))
    fm = parse_skill_frontmatter(text)
    name, desc = (fm.get("name") or "").strip(), (fm.get("description") or "").strip()
    if name and desc:
        rv.check("Frontmatter name + description", "pass", f"name `{F.safe_text(name, 60)}`")
    else:
        missing = " and ".join(x for x, v in (("name", name), ("description", desc)) if not v)
        rv.check("Frontmatter name + description", "warn", f"missing {missing}")
        rv.findings.append(f"frontmatter missing {missing}")
    lic_meta = meta.get("license") or None
    tier, label, evidence = scout.license_tier({"license": {"spdx_id": (lic_meta or {}).get("spdx_id"),
                                                            "name": (lic_meta or {}).get("name"),
                                                            "key": (lic_meta or {}).get("key")} if lic_meta else None},
                                               folder, lic_files, fm.get("license") or None)
    rv.inputs["license"] = {"tier": tier, "label": label, "evidence": evidence[:4]}
    if tier == "pass":
        rv.check("License", "pass", F.safe_text(label, 40))
    elif tier == "license_review":
        rv.check("License", "warn", "needs human review: " + F.safe_text(", ".join(evidence[:3]), 160))
    else:
        rv.check("License", "fail", "no license evidence (no SPDX id, license file or frontmatter)")
    cand = {"issue": number, "identity": ident, "owner": owner, "repo": repo, "subpath": folder,
            "skill_path": path, "default_branch": branch, "stars": meta.get("stargazers_count"),
            "_skill": {"files": scout.folder_files(entries, folder)}}
    try:
        sr = scout.run_scan_with_client(client, cand, None)
    except scout.GitHubError as e:
        rv.outcome = "transient"
        rv.findings.append(f"scan fetch error: {type(e).__name__}")
        return
    sev = sr.get("max_severity") or "none"
    counts = sr.get("finding_counts") or {}
    ctext = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no findings"
    if sr.get("status") != "pass":
        rv.critical = True
        rv.check("Static scan", "fail", f"{sr.get('hold') or 'hold'}; max severity {sev}; {ctext}")
        rv.findings.append(f"static scan hold: {sr.get('hold')}")
    else:
        rv.check("Static scan", "warn" if sev == "high" else "pass", f"max severity {sev}; {ctext}; {sr.get('files_scanned')} files")
    rv.inputs.update({"skill_md": text, "scan": {"max_severity": sev, "finding_counts": counts,
                                                 "files_scanned": sr.get("files_scanned")}})
    if catalog_idx is None:
        rv.outcome = "transient"  # never decide without the duplicate check
        rv.findings.append("catalog unavailable: duplicate check not possible")
    else:
        dup = next((d for d in (scout.catalog_match(catalog_idx, k, folder, dup_idx_total)
                                for k in dict.fromkeys([rk, full.lower()])) if d), None)
        if dup:
            rv.check("Catalog duplicate", "fail", f"already listed ({dup})")
            rv.reject = "reject:duplicate"
        else:
            rv.check("Catalog duplicate", "pass", "not listed")
    if tier == "reject" and not rv.reject:
        rv.reject = "reject:no-license"
    rv.inputs["license_tier"] = tier


def check_clawhub(ch: Any, feed_by_id: dict[str, Any] | None, blk: dict[str, Any], catalog: dict | None,
                  rv: Review) -> None:
    from sources import clawhub as C
    ident = blk["psk-id"]
    body = ident[len("clawhub:"):]
    owner, _, slug = body.lstrip("@").partition("/")
    url = C.page_url(owner, slug)
    entry = (feed_by_id or {}).get(f"@{owner}/{slug}".lower())
    version = (entry or {}).get("version") or (blk.get("metrics") or {}).get("version")
    try:
        page = ch.page(owner, slug, version)
    except PermissionError as e:
        rv.outcome = "transient"
        rv.findings.append(f"ClawHub fetch not allowed: {e}")
        return
    page = page or {}
    if not page.get("ok"):
        if page.get("status") in (404, 410):
            rv.outcome = "not_found"
            rv.check("Source exists", "fail", f"{url} returns {page.get('status')}")
        else:
            rv.outcome = "transient"
            rv.findings.append(f"ClawHub page status {page.get('status')}")
        return
    rv.check("Source exists", "pass", url)
    integ = (((entry or {}).get("install") or {}).get("candidates") or [{}])[0].get("integrity")
    rv.sha = "clawhub:" + str(version or "?") + (":" + str(integ).replace("sha256:", "")[:16] if integ else "")
    old = F.block_sha(blk)
    rv.check("Skill sha", "pass" if rv.sha == old else "warn",
             f"`{rv.sha}`" + ("" if rv.sha == old else f" (block had `{old}`)"))
    text = page.get("skill_md_text")
    if not text:
        rv.check("SKILL.md at path", "fail", "no SKILL.md text on the public page")
        rv.reject = "reject:no-skill-md"
        return
    rv.check("SKILL.md at path", "pass", f"public page SKILL.md ({len(text):,} chars)")
    name = (entry or {}).get("title")
    desc = (entry or {}).get("description") or page.get("parsed_description")
    if name and desc:
        rv.check("Frontmatter name + description", "pass", f"name `{F.safe_text(name, 60)}` (ClawHub feed)")
    else:
        rv.check("Frontmatter name + description", "warn", "name or description missing in the ClawHub feed")
        rv.findings.append("name/description missing")
    pl = page.get("page_license")
    if pl and pl.upper() != "MIT-0":
        rv.check("License", "warn", f"page declares {F.safe_text(pl, 30)} (platform default MIT-0)")
        tier = "license_review"
    else:
        rv.check("License", "pass", f"MIT-0 (ClawHub platform license; evidence {C.LICENSE_EVIDENCE_URL})")
        tier = "pass"
    rv.inputs["license_tier"] = tier
    sr = C.scan_skill_md(text)
    scripts = [f for f in page.get("files") or [] if f.lower().endswith(C.SCRIPT_EXT)]
    ctext = ", ".join(f"{k} {v}" for k, v in sorted((sr.get("finding_counts") or {}).items())) or "no findings"
    if sr["critical"]:
        rv.critical = True
        rv.check("Static scan", "fail", f"critical finding in SKILL.md; {ctext}")
    elif scripts:
        rv.critical = True
        rv.check("Static scan", "fail", f"{len(scripts)} bundle script(s) cannot be scanned (not reachable without /api/)")
    else:
        rv.check("Static scan", "warn" if sr["max_severity"] == "high" else "pass",
                 f"max severity {sr['max_severity']}; {ctext} (SKILL.md text only)")
    mod = page.get("moderation") or {}
    crit = int(page.get("clawhub_critical_findings") or 0)
    if mod.get("flagged") or crit:
        rv.critical = True
        bits = [k for k in ("is_suspicious", "malware_blocked", "hidden_by_mod") if mod.get(k)]
        bits += [f"scanner verdicts: {', '.join(mod.get('scanner_verdicts') or [])}"] if mod.get("scanner_verdicts") else []
        bits += [f"{crit} critical platform finding(s)"] if crit else []
        rv.check("ClawHub platform flags", "fail", "; ".join(bits))
    else:
        rv.check("ClawHub platform flags", "pass", "no suspicious / malware / hidden flags")
    rv.inputs.update({"skill_md": text, "scan": {"max_severity": sr["max_severity"],
                                                 "finding_counts": sr.get("finding_counts") or {}}})
    if catalog is None:
        rv.outcome = "transient"
        rv.findings.append("catalog unavailable: duplicate check not possible")
    else:
        full, slugs = C.clawhub_catalog_index(catalog)
        if f"{owner.lower()}/{slug.lower()}" in full or slug.lower() in slugs:
            rv.check("Catalog duplicate", "fail", "ClawHub slug already listed")
            rv.reject = "reject:duplicate"
        else:
            rv.check("Catalog duplicate", "pass", "not listed")


def decide(rv: Review) -> None:
    """Verdict from check results. Order matters: deterministic critical findings are never overridden."""
    if rv.reject in ("reject:duplicate", "reject:no-skill-md"):
        rv.verdict = "review:reject"
        return
    if rv.critical:
        rv.verdict = "review:hold-critical"
        return
    if rv.reject:
        rv.verdict = "review:reject"
        return
    if rv.kind == "large-collection":
        rv.verdict = "review:large-collection"
        return
    if rv.inputs.get("license_tier") == "license_review":
        rv.verdict = "review:license-review"
        return
    warn = [c for c, (s, _) in rv.checks.items() if s == "warn" and c != "Skill sha"]
    rv.verdict = "review:needs-ai" if warn else "review:pass"


# --------------------------------------------------------------------------- rendering

ICON = {"pass": "pass", "fail": "FAIL", "warn": "warn", "n/a": "n/a"}


def render_comment(rv: Review, ai_section: str | None = None) -> str:
    label = rv.verdict + (f" + {rv.reject}" if rv.reject else "")
    rows = ["| Check | Result | Detail |", "|---|---|---|"]
    for c in CHECKS:
        s, d = rv.checks[c]
        rows.append(f"| {c} | {ICON[s]} | {d or ''} |")
    parts = [F.review_marker(rv.identity, rv.sha, rv.verdict),
             f"**ProSkills automated review** (deterministic, no AI). Verdict: `{label}`", "",
             "\n".join(rows)]
    if rv.findings:
        parts += ["", "Findings:"] + [f"- {F.safe_text(f, 200)}" for f in rv.findings[:10]]
    when = F.utc_now().astimezone(F.DHAKA).strftime("%Y-%m-%d %H:%M")
    parts += ["", f"Reviewed {when} (UTC+6) at skill sha `{(rv.sha or 'none')[:16]}`. This comment is edited in "
                  "place on re-review; a new commit to the skill folder triggers a new review."]
    if ai_section:
        parts += ["", ai_section]
    return "\n".join(parts)


def ai_section_of(body: str | None) -> str | None:
    b = body or ""
    i, j = b.find(F.AI_START), b.find(F.AI_END)
    if i >= 0 and j > i:
        return b[i: j + len(F.AI_END)]
    return None


# --------------------------------------------------------------------------- runner

class Reviewer:
    def __init__(self, api: F.IssueRepo, state_path: Path, *, apply: bool, catalog: dict | None,
                 clawhub_client: Any = None, feed_by_id: dict | None = None, limit: int = 50,
                 log=lambda m: print(m, flush=True), now=None):
        self.api = api
        self.client = api.client
        self.state_path = state_path
        self.state: dict[str, Any] = F.load_json(state_path, {}) or {}
        self.apply = apply
        self.catalog = catalog
        self.catalog_idx = scout.build_catalog_index(catalog) if catalog is not None else None
        self.ch = clawhub_client
        self.feed_by_id = feed_by_id
        self.limit = limit
        self.log = log
        self.now = now or F.utc_now()
        self.results: list[dict[str, Any]] = []
        self.counts: Counter = Counter()

    def select(self, issues: list[dict[str, Any]], only: list[int] | None = None) -> list[tuple[dict, dict]]:
        out = []
        for i in sorted(issues, key=lambda x: int(x["number"])):
            n = int(i["number"])
            if only and n not in only:
                continue
            if F.untouchable(i):
                self.counts["skip_untouchable"] += 1
                continue
            labs = F.issue_labels(i)
            if F.LABEL_CANDIDATE not in labs and not only:
                continue
            blk = F.parse_block(i.get("body"))
            if blk is None:
                e = next((v for v in self._index_entries() if v.get("issue") == n and v.get("block")), None)
                blk = e["block"] if e else None
            if not blk:
                self.counts["skip_no_block"] += 1
                continue
            st = self.state.get(str(n)) or {}
            verdict = F.verdict_of(labs)
            pending_nf = st.get("not_found_first_at")
            seen_sha = st.get("block_sha") or st.get("sha")
            changed = bool(seen_sha) and seen_sha != F.block_sha(blk)
            due = (not verdict or changed
                   or (pending_nf and self.now - F.parse_iso(pending_nf) >= NOT_FOUND_CONFIRM) or bool(only))
            if verdict == "review:hold-ai" and not changed and not only:
                due = False
            if due:
                out.append((i, blk))
        return out[: self.limit]

    def _index_entries(self) -> list[dict[str, Any]]:
        idx = F.load_json(self.state_path.parent / "issue-index.json", {}) or {}
        return list((idx.get("identities") or {}).values())

    def review_one(self, issue: dict[str, Any], blk: dict[str, Any]) -> dict[str, Any]:
        n = int(issue["number"])
        rv = Review(blk["psk-id"], blk.get("kind") or "skill")
        if blk["psk-id"].startswith("clawhub:"):
            if self.ch is None:
                rv.outcome = "transient"
                rv.findings.append("ClawHub client unavailable")
            else:
                check_clawhub(self.ch, self.feed_by_id, blk, self.catalog, rv)
        else:
            check_github(self.client, blk, self.catalog_idx, rv, n)
        st = self.state.setdefault(str(n), {})
        res = {"issue": n, "identity": rv.identity, "sha": rv.sha}
        if rv.outcome == "transient":
            self.counts["transient"] += 1
            return {**res, "action": "skip_transient", "findings": rv.findings}
        if rv.outcome == "not_found":
            first = F.parse_iso(st.get("not_found_first_at"))
            if not first:
                st["not_found_first_at"] = F.iso(self.now)
                self.counts["not_found_pending"] += 1
                return {**res, "action": "not_found_pending"}
            if self.now - first < NOT_FOUND_CONFIRM:
                self.counts["not_found_pending"] += 1
                return {**res, "action": "not_found_pending"}
            rv.reject = "reject:not-found"
            rv.verdict = "review:reject"
            rv.findings.append(f"not found on two checks: {st['not_found_first_at']} and {F.iso(self.now)}")
        else:
            st.pop("not_found_first_at", None)
            decide(rv)
        ai = st.get("ai") or {}
        ai_kept = rv.verdict == "review:needs-ai" and ai.get("sha") and ai.get("sha") == rv.sha
        if ai_kept:  # same sha already judged by ai_review.py: keep its outcome (never for critical/reject)
            rv.verdict = ai["label"]
        labs = F.issue_labels(issue)
        cur_verdict = F.verdict_of(labs)
        cur_reject = sorted(l for l in labs if l.startswith("reject:"))
        want_labels = [rv.verdict] + ([rv.reject] if rv.reject else []) + ([F.LABEL_AI_REVIEWED] if ai_kept else [])
        # find our comment (state first, then scan comments once)
        cid = st.get("comment_id")
        old_body = None
        if self.apply or not cid:
            comments = self.api.list_comments(n) if not cid else []
            for cm in comments:  # the LAST marker comment wins (see IssueRepo.edit_or_create_comment)
                mk = F.parse_review_marker(cm.get("body"))
                if mk:
                    cid, old_body = cm["id"], cm.get("body")
        old_marker = F.parse_review_marker(old_body) if old_body else (
            {"sha": st.get("sha") or "none", "verdict": st.get("verdict")} if cid else None)
        unchanged = (old_marker is not None and old_marker.get("verdict") == rv.verdict
                     and old_marker.get("sha") == (rv.sha or "none") and cur_verdict == rv.verdict
                     and cur_reject == ([rv.reject] if rv.reject else []))
        close = rv.reject in CLOSE_REASONS
        res.update({"verdict": rv.verdict, "reject": rv.reject, "previous": cur_verdict,
                    "findings": rv.findings[:10], "checks": {k: v[0] for k, v in rv.checks.items()}})
        if unchanged:
            if self.apply:
                st["block_sha"] = F.block_sha(blk)
            self.counts["unchanged"] += 1
            res["action"] = "unchanged"
            return res
        keep_ai = ai_section_of(old_body) if old_marker and old_marker.get("sha") == (rv.sha or "none") else None
        body = render_comment(rv, keep_ai)
        assert F.no_handles(body), "review comment would contain an @-handle"
        res["action"] = "edit_comment" if cid else "comment"
        res["close"] = close
        self.counts[rv.verdict] += 1
        if self.apply:
            cid = self.api.edit_or_create_comment(n, cid, body)
            F.set_labels(self.api, issue, want_labels, ("review:", "reject:"))
            if close:
                self.api.close_not_planned(n)
            st.update({"psk_id": rv.identity, "sha": rv.sha, "block_sha": F.block_sha(blk),
                       "verdict": rv.verdict, "reject": rv.reject,
                       "comment_id": cid, "reviewed_at": F.iso(self.now), "critical": rv.critical,
                       "inputs_digest": {"license": rv.inputs.get("license"), "scan": rv.inputs.get("scan")}})
        return res

    def run(self, issues: list[dict[str, Any]], only: list[int] | None = None) -> dict[str, Any]:
        todo = self.select(issues, only)
        self.log(f"  candidate issues={len(issues)} due={len(todo)} (limit {self.limit})")
        stopped = None
        for issue, blk in todo:
            try:
                self.api.check_reserve()
            except F.ReserveLow as e:
                stopped = str(e)
                break
            try:
                self.results.append(self.review_one(issue, blk))
            except scout.RateLimitExhausted as e:
                stopped = f"rate limit: {e}"
                break
            if self.apply:
                F.save_json(self.state_path, self.state)
        F.save_json(self.state_path, self.state) if self.apply else None
        return {"summary": {"mode": "apply" if self.apply else "dry-run", "reviewed": len(self.results),
                            "counts": dict(self.counts), "stopped": stopped,
                            "core_remaining": self.client.core_remaining, "writes": len(self.api.writes)},
                "results": self.results}


def load_clawhub(state_dir: Path):
    from sources import clawhub as C
    from sources.base import DiskCache
    cache = DiskCache(state_dir / "review-clawhub-cache.json")
    ch = C.ClawHubClient(cache, page_ttl_s=86400)
    ch.load_robots()
    feed = ch.feed() or {}
    by_id = {str(e.get("id") or "").lower(): e for e in feed.get("entries") or []}
    return ch, by_id, cache


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic review of candidate issues")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--issues", default=None, help="comma-separated issue numbers (forces re-check)")
    ap.add_argument("--catalog", type=Path, default=None, help="catalog snapshot (default: live)")
    ap.add_argument("--core-reserve", type=int, default=1500)
    ap.add_argument("--state-dir", type=Path, default=F.STATE_DIR)
    ap.add_argument("--ignore-routine-windows", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    args.limit = max(1, min(50, args.limit))
    if not args.ignore_routine_windows and F.in_routine_window():
        print("WINDOW_SKIP: inside an hourly routine window (Dhaka :10-:26 / :40-:58)", flush=True)
        return 0
    held = F.live_routine_lock()
    if held:
        print(f"LOCK_SKIP: {held}", flush=True)
        return 0
    with scout.scout_state_lock(args.state_dir):
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    t0 = time.time()
    client = scout.GitHubClient(scout.get_gh_token(), throttle=scout.AdaptiveThrottle(4))
    api = F.IssueRepo(client, core_reserve=args.core_reserve)
    try:
        catalog = load_catalog(args.catalog)
    except Exception as e:  # duplicate check degrades to "warn" -> needs-ai, never a pass
        print(f"  catalog unavailable: {e}", flush=True)
        catalog = None
    only = [int(x) for x in args.issues.split(",") if x.strip()] if args.issues else None
    if only:
        issues = [api.get_issue(n) for n in only]
        issues = [i for i in issues if i.get("state") == "open" and "pull_request" not in i]
    else:
        issues = api.list_issues(state="open", labels=F.LABEL_CANDIDATE)
    ch = feed = cache = None
    if any((F.parse_block(i.get("body")) or {}).get("psk-id", "").startswith("clawhub:") for i in issues):
        try:
            ch, feed, cache = load_clawhub(args.state_dir)
        except Exception as e:
            print(f"  ClawHub unavailable: {e}", flush=True)
    rv = Reviewer(api, args.state_dir / STATE_NAME, apply=args.apply, catalog=catalog, clawhub_client=ch,
                  feed_by_id=feed, limit=args.limit)
    out = rv.run(issues, only)
    if cache is not None:
        cache.save()
    out["summary"]["elapsed_s"] = round(time.time() - t0, 1)
    stamp = scout.now_dhaka().strftime("%Y-%m-%d-%H%M")
    path = args.out or scout.ART / f"review-{stamp}{'' if args.apply else '-dryrun'}.json"
    F.save_json(path, out)
    print(json.dumps(out["summary"], indent=2), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
