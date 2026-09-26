#!/usr/bin/env python3
"""File skill candidates as GitHub issues (deterministic; no AI). Default --dry-run; --apply writes.

Inputs (unified candidate records):
  * scout.py queue      operator/state/artifacts/candidate-queue-YYYY-MM-DD.json (eligible + license_review +
                        large_collections): candidates that already have a (legacy) issue.
  * source adapters     operator/state/artifacts/source-candidates-YYYY-MM-DD.json (source_candidates.py --write):
                        GitHub topics / new repos / awesome lists / known orgs / ClawHub.

Dedupe by identity (github:owner/repo[::subpath] | clawhub:@owner/slug), in this order:
  1. the `<!-- proskills:candidate v1 ... psk-id: ... -->` block in an issue body (or marker comment),
     mirrored in operator/state/scout/issue-index.json;
  2. the issue number carried by a scout.py record;
  3. normalized repo URL (and ClawHub page URL) of legacy (old-flow) issues, open and closed.
A legacy match gets the machine block (body edit when we may edit, else ONE marker comment) plus labels
candidate + legacy:v0 + scout:filed. A closed legacy match is never re-filed. Anything else is a new issue
`[Candidate] owner/repo[/subpath] - <name>` labelled candidate + source:<type> + scout:filed.
On a new skill sha the block is edited in place and review:* / reject:* / ai:* labels are removed.

Caps: <= --max-new (25) new issues/run, <= 150/day, <= 3 new per repo/day, no new issues while more than
200 open candidate issues lack a review:* label, >= 3 s between creates, stop when REST core < reserve.
Never touches protected/critical/publisher-skip issues or issues labelled blocked:* / published.
Never @-mentions anyone (all third-party text goes through issue_flow.safe_text).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scout  # noqa: E402
import issue_flow as F  # noqa: E402

ART = scout.ART
INDEX_NAME = "issue-index.json"
DEFAULT_CAPS = {"max_new": 25, "max_new_day": 150, "max_new_repo_day": 3, "max_unreviewed": 200,
                "max_legacy": 25, "max_refresh": 25}
FILE_STATUSES = {"pass", "license_review", "large_collection", None}
CLAWHUB_URL_RE = re.compile(r"clawhub\.ai/(?:(@?[A-Za-z0-9_.-]+)/skills/([A-Za-z0-9_.-]+)|skills/([A-Za-z0-9_.-]+)|@([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+))", re.I)


# --------------------------------------------------------------------------- inputs

def load_scout_records(path: Path | None) -> list[dict[str, Any]]:
    q = F.load_json(path, {}) if path else {}
    out: list[dict[str, Any]] = []
    held = {str(h.get("identity") or "").lower() for key in ("holds_critical_static", "holds_other_scan")
            for h in q.get(key) or [] if isinstance(h, dict)}
    seen: set[str] = set()
    for key in ("passed", "eligible", "license_review"):
        for r in q.get(key) or []:
            if not isinstance(r, dict) or not r.get("identity") or not r.get("issue"):
                continue
            ident = str(r["identity"]).lower()
            if ident in held or ident in seen:
                continue
            seen.add(ident)
            c = F.normalize({**r, "status": "license_review" if key == "license_review" else "pass"}, "scout")
            if c:
                out.append(c)
    for lc in q.get("large_collections") or []:
        if isinstance(lc, dict) and lc.get("repo") and lc.get("issue"):
            ident = f"github:{str(lc['repo']).lower()}"
            if ident in seen:
                continue
            seen.add(ident)
            c = F.normalize({"identity": ident, "issue": lc["issue"], "stars": lc.get("stars"),
                             "skills_total": lc.get("skills_total"), "license": lc.get("license"),
                             "license_spdx": lc.get("license"), "status": "large_collection"}, "scout_large")
            if c:
                out.append(c)
    return out


def load_source_records(path: Path | None) -> list[dict[str, Any]]:
    data = F.load_json(path, {}) if path else {}
    recs = data.get("candidates") if isinstance(data, dict) else data
    out = []
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        if r.get("status") not in ("pass", "license_review", "large_collection"):
            continue
        c = F.normalize(r, "sources")
        if c:
            out.append(c)
    return out


def default_input(prefix: str, art: Path = ART) -> Path | None:
    today = scout.now_dhaka().date()
    for d in (today, today - timedelta(days=1)):
        p = art / f"{prefix}-{d.isoformat()}.json"
        if p.exists():
            return p
    return None


# --------------------------------------------------------------------------- index

class IssueIndex:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = F.load_json(path, {}) or {}
        self.data.setdefault("version", 1)
        self.data.setdefault("identities", {})
        self.data.setdefault("filed", [])
        self.data.setdefault("legacy_closed", {"synced_at": None, "repos": {}, "clawhub": {}})

    @property
    def identities(self) -> dict[str, Any]:
        return self.data["identities"]

    def put(self, ident: str, **fields: Any) -> None:
        e = self.identities.setdefault(ident, {})
        e.update({k: v for k, v in fields.items() if v is not None})
        e["updated_at"] = F.iso()

    def filed_today(self, day: str) -> list[dict[str, Any]]:
        return [f for f in self.data["filed"] if f.get("day") == day]

    def record_filed(self, issue: int, ident: str, repo_key: str, day: str) -> None:
        self.data["filed"].append({"issue": issue, "identity": ident, "repo_key": repo_key, "day": day, "at": F.iso()})

    def prune(self, keep_days: int = 8) -> None:
        cutoff = (F.utc_now() - timedelta(days=keep_days)).astimezone(F.DHAKA).date().isoformat()
        self.data["filed"] = [f for f in self.data["filed"] if (f.get("day") or "") >= cutoff]

    def save(self) -> None:
        F.save_json(self.path, self.data)


def clawhub_keys(text: str) -> set[str]:
    keys = set()
    for m in CLAWHUB_URL_RE.finditer(text or ""):
        owner = (m.group(1) or m.group(4) or "").lstrip("@").lower()
        slug = (m.group(2) or m.group(3) or m.group(5) or "").lower()
        if slug:
            keys.add(f"slug:{slug}")
            if owner:
                keys.add(f"{owner}/{slug}")
    return keys


class IssueView:
    """What is on GitHub right now: blocks by identity, legacy targets by identity / repo / ClawHub slug."""

    def __init__(self, open_issues: list[dict[str, Any]], index: IssueIndex):
        self.by_number: dict[int, dict[str, Any]] = {}
        self.block_by_ident: dict[str, int] = {}
        self.block_of: dict[int, dict[str, Any]] = {}
        self.legacy_ident: dict[str, list[int]] = {}
        self.legacy_repo: dict[str, list[int]] = {}
        self.legacy_claw: dict[str, list[int]] = {}
        self.unreviewed_candidates = 0
        for i in open_issues:
            n = int(i["number"])
            self.by_number[n] = i
            blk = F.parse_block(i.get("body"))
            if blk is None:
                e = next((v for v in index.identities.values() if v.get("issue") == n and v.get("via") == "comment"), None)
                if e and e.get("block"):
                    blk = e["block"]
            labs = F.issue_labels(i)
            if blk:
                self.block_of[n] = blk
                self.block_by_ident.setdefault(blk["psk-id"], n)
                if F.LABEL_CANDIDATE in labs and not F.verdict_of(labs):
                    self.unreviewed_candidates += 1
                continue
            t = scout.issue_target(i)
            if t:
                self.legacy_ident.setdefault(t.identity, []).append(n)
                self.legacy_repo.setdefault(t.repo_key, []).append(n)
            for k in clawhub_keys(f"{i.get('title') or ''}\n{i.get('body') or ''}"):
                self.legacy_claw.setdefault(k, []).append(n)


def sync_closed(api: F.IssueRepo, index: IssueIndex, log) -> None:
    """Incrementally index closed issues: blocks (closed candidates) + legacy repo/ClawHub keys."""
    lc = index.data["legacy_closed"]
    since = lc.get("synced_at")
    started = F.iso()
    items = api.list_issues(state="closed", since=since, max_pages=80)
    for i in items:
        n = int(i["number"])
        blk = F.parse_block(i.get("body"))
        if blk:
            index.put(blk["psk-id"], issue=n, state="closed")
            continue
        t = scout.issue_target(i)
        if t:
            lst = lc["repos"].setdefault(t.repo_key, [])
            if n not in lst:
                lst.append(n)
            if t.subpath:
                lst2 = lc.setdefault("idents", {}).setdefault(t.identity, [])
                if n not in lst2:
                    lst2.append(n)
        for k in clawhub_keys(f"{i.get('title') or ''}\n{i.get('body') or ''}"):
            lst = lc["clawhub"].setdefault(k, [])
            if n not in lst:
                lst.append(n)
    lc["synced_at"] = started
    log(f"  closed issues synced: {len(items)} (since {since or 'beginning'})")


# --------------------------------------------------------------------------- live sha

def _folder(c: dict[str, Any]) -> str | None:
    sp = str(c.get("skill_path") or "")
    if "/" in sp:
        return sp.rsplit("/", 1)[0]
    return None


def resolve_shas(client: scout.GitHubClient, c: dict[str, Any]) -> str:
    """Fill commit_sha / skill_tree_sha / default_branch for a GitHub candidate. Returns ok|not_found|transient."""
    if c["source_type"] != "github":
        return "ok"
    owner, repo = F.repo_key_of(c["identity"]).split("/", 1)
    try:
        branch = c.get("default_branch")
        if not branch:
            branch = (client.rest(f"repos/{owner}/{repo}") or {}).get("default_branch") or "main"
            c["default_branch"] = branch
        com = client.rest(f"repos/{owner}/{repo}/commits/{quote(branch, safe='')}") or {}
        c["commit_sha"] = com.get("sha")
        root_tree = ((com.get("commit") or {}).get("tree") or {}).get("sha")
        folder = _folder(c) if c["kind"] == "skill" else None
        if not folder:
            c["skill_tree_sha"] = root_tree
            return "ok" if root_tree else "transient"
        parent = str(PurePosixPath(folder).parent)
        parent = "" if parent == "." else parent
        listing = client.rest(f"repos/{owner}/{repo}/contents/{quote(parent)}?ref={c['commit_sha']}") or []
        name = PurePosixPath(folder).name
        hit = next((e for e in listing if isinstance(e, dict) and e.get("type") == "dir"
                    and str(e.get("name")) == name), None)
        if hit is None:
            hit = next((e for e in listing if isinstance(e, dict) and e.get("type") == "dir"
                        and str(e.get("name")).lower() == name.lower()), None)
        if not hit:
            return "not_found"
        c["skill_tree_sha"] = hit.get("sha")
        return "ok"
    except scout.NotFound:
        return "not_found"
    except scout.GitHubError:
        return "transient"


# --------------------------------------------------------------------------- rendering

def _num(v: Any) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "n/a"


def issue_title(c: dict[str, Any]) -> str:
    if c["kind"] == "large-collection":
        return f"[Candidate] {F.display_path(c)} - large collection ({_num(c.get('skills_total'))} SKILL.md files)"[:200]
    return f"[Candidate] {F.display_path(c)} - {F.safe_text(c.get('name') or 'skill', 60)}"[:200]


def issue_body(c: dict[str, Any], block: str) -> str:
    lic = c.get("license") or {}
    m = c.get("metrics") or {}
    if c["source_type"] == "clawhub":
        pop = f"{_num(m.get('downloads'))} downloads, {_num(m.get('installs'))} installs (ClawHub)"
        src = c.get("source_url") or c.get("repo_url")
    else:
        pop = f"{_num(m.get('stars'))} stars, {_num(m.get('forks'))} forks, last push {str(m.get('pushed_at') or 'n/a')[:10]}"
        src = c.get("source_url") or c.get("repo_url")
    found = ", ".join(sorted({F.safe_text(str(s.get("source") or "?"), 40) for s in c.get("sources") or []})) or "scout"
    lic_txt = f"{F.safe_text(lic.get('spdx') or lic.get('label') or 'unknown', 40)} ({lic.get('tier') or 'n/a'})"
    rows = [("Skill", F.safe_text(c.get("name") or "", 80)), ("Source", src or "n/a"),
            ("SKILL.md", f"`{c['skill_path']}`" if c.get("skill_path") else "n/a"),
            ("License", lic_txt), ("Popularity", pop), ("Found via", found)]
    if c["kind"] == "large-collection":
        rows[2] = ("SKILL.md files", _num(c.get("skills_total")))
    table = "\n".join(["| | |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows])
    desc = F.safe_text(c.get("description") or "", 280)
    parts = ["Skill candidate filed by the ProSkills scout (deterministic, no AI).", "", table]
    if desc:
        parts += ["", f"> {desc}"]
    if c["kind"] == "large-collection":
        parts += ["", "This repository has more than 50 SKILL.md files. It is tracked as one collection issue and is "
                      "not auto-published skill by skill."]
    parts += ["", "Next: the automated review posts one checklist comment and sets a `review:*` label.", "", block]
    return "\n".join(parts)


MARKER_COMMENT_INTRO = "Machine-readable candidate block for the ProSkills issue flow (no action needed)."


# --------------------------------------------------------------------------- core

class ScoutFiler:
    def __init__(self, api: F.IssueRepo, index: IssueIndex, *, apply: bool, caps: dict[str, int] | None = None,
                 log=lambda m: print(m, flush=True), file_siblings: bool = False):
        self.api = api
        self.client = api.client
        self.index = index
        self.apply = apply
        self.caps = {**DEFAULT_CAPS, **(caps or {})}
        self.log = log
        self.file_siblings = file_siblings
        self.actions: list[dict[str, Any]] = []
        self.skips: Counter = Counter()
        self.skip_examples: dict[str, list[str]] = {}
        self.stopped: str | None = None
        self.can_edit = False

    def _skip(self, c: dict[str, Any], reason: str) -> None:
        key = reason.split(":", 1)[0]
        self.skips[key] += 1
        ex = self.skip_examples.setdefault(key, [])
        if len(ex) < 5:
            ex.append(f"{c['identity']} ({reason})")

    def _act(self, kind: str, c: dict[str, Any], issue: int | None, **extra: Any) -> None:
        self.actions.append({"action": kind, "identity": c["identity"], "issue": issue,
                             "title": extra.pop("title", None), **extra})

    def run(self, candidates: list[dict[str, Any]], *, sync_closed_issues: bool = True) -> dict[str, Any]:
        now = F.utc_now()
        day = F.dhaka_day(now)
        self.can_edit = self.api.can_edit_bodies()
        open_issues = self.api.list_issues(state="open")
        if sync_closed_issues:
            sync_closed(self.api, self.index, self.log)
        view = IssueView(open_issues, self.index)
        # index <- GitHub (source of truth for open blocks)
        for n, blk in view.block_of.items():
            prev = self.index.identities.get(blk["psk-id"], {})
            self.index.put(blk["psk-id"], issue=n, state="open", sha=F.block_sha(blk),
                           via=prev.get("via") or "body")
        self.log(f"  open issues={len(open_issues)} with_block={len(view.block_of)} "
                 f"unreviewed_candidates={view.unreviewed_candidates} can_edit_bodies={self.can_edit}")

        # dedupe incoming records by identity (scout records first: they carry the legacy issue)
        uniq: dict[str, dict[str, Any]] = {}
        for c in candidates:
            if c["status"] not in FILE_STATUSES:
                self._skip(c, f"status:{c['status']}")
                continue
            prev = uniq.get(c["identity"])
            if prev is None:
                uniq[c["identity"]] = c
            else:  # merge sources / keep the issue from the scout record
                prev["sources"] = (prev.get("sources") or []) + [s for s in c.get("sources") or []
                                                                 if s not in (prev.get("sources") or [])]
                prev["issue"] = prev.get("issue") or c.get("issue")
                for k in ("commit_sha", "skill_tree_sha", "description", "name"):
                    prev[k] = prev.get(k) or c.get(k)

        refresh, legacy, new = [], [], []
        for c in uniq.values():
            ident = c["identity"]
            if ident in view.block_by_ident:
                refresh.append((c, view.block_by_ident[ident]))
                continue
            ie = self.index.identities.get(ident) or {}
            if ie.get("state") == "closed":
                self._skip(c, f"closed_candidate_issue:#{ie.get('issue')}")
                continue
            target, why = self._legacy_target(c, view)
            if why:
                self._skip(c, why)
                continue
            if target:
                legacy.append((c, target))
            else:
                new.append(c)

        self._do_refresh(refresh, view)
        self._do_legacy(legacy, view)
        self._do_new(new, view, day)
        self.index.prune()
        summary = {"mode": "apply" if self.apply else "dry-run", "day_dhaka": day,
                   "candidates_in": len(candidates), "unique": len(uniq),
                   "counts": dict(Counter(a["action"] for a in self.actions)),
                   "skips": dict(self.skips), "skip_examples": self.skip_examples,
                   "stopped": self.stopped, "unreviewed_candidates": view.unreviewed_candidates,
                   "filed_today": len(self.index.filed_today(day)),
                   "core_remaining": self.client.core_remaining, "writes": len(self.api.writes)}
        return {"summary": summary, "actions": self.actions}

    # -- matching
    def _legacy_target(self, c: dict[str, Any], view: IssueView) -> tuple[int | None, str | None]:
        cands: list[int] = []
        lc = self.index.data["legacy_closed"]
        if c.get("issue"):
            n = int(c["issue"])
            if n in view.by_number:
                cands.append(n)
            else:
                return None, f"scout_issue_not_open:#{n}"
        if c["source_type"] == "github":
            rk = F.repo_key_of(c["identity"])
            cands += view.legacy_ident.get(c["identity"], [])
            repo_issues = view.legacy_repo.get(rk, [])
            sub = (c["identity"].split("::", 1)[1] if "::" in c["identity"] else "")
            for n in repo_issues:
                t = scout.issue_target(view.by_number[n])
                if t and scout._covers(t.subpath, sub):
                    cands.append(n)
            if not cands:
                if rk in view.legacy_repo:
                    return None, f"repo_has_other_legacy_issue:#{view.legacy_repo[rk][0]}"
                closed = (lc.get("idents") or {}).get(c["identity"]) or lc["repos"].get(rk)
                if closed:
                    return None, f"legacy_closed:#{closed[0]}"
                # another open candidate issue (new flow) already claims this repo -> sibling skill
                for ident, n in view.block_by_ident.items():
                    if F.repo_key_of(ident) == rk and not self.file_siblings and c["kind"] == "skill" \
                            and view.block_of[n].get("kind") == "large-collection":
                        return None, f"repo_is_large_collection:#{n}"
                return None, None
        else:
            owner = (c.get("owner") or "").lower()
            slug = (c.get("repo") or "").lower()
            for k in (f"{owner}/{slug}", f"slug:{slug}"):
                cands += view.legacy_claw.get(k, [])
            if not cands:
                for k in (f"{owner}/{slug}", f"slug:{slug}"):
                    if lc["clawhub"].get(k):
                        return None, f"legacy_closed:#{lc['clawhub'][k][0]}"
                return None, None
        for n in sorted(dict.fromkeys(cands)):
            issue = view.by_number[n]
            why = F.untouchable(issue)
            if why:
                return None, f"legacy_untouchable:#{n}:{why}"
            blk = view.block_of.get(n)
            if blk and blk["psk-id"] != c["identity"]:
                if self.file_siblings:
                    continue
                return None, f"repo_claimed_by_issue:#{n}"
            return n, None
        return None, "sibling_skill_deferred"

    # -- actions
    def _reserve_ok(self) -> bool:
        try:
            self.api.check_reserve()
            return True
        except F.ReserveLow as e:
            self.stopped = str(e)
            return False

    def _do_refresh(self, items: list[tuple[dict[str, Any], int]], view: IssueView) -> None:
        done = 0
        for c, n in items:
            issue = view.by_number[n]
            if F.untouchable(issue):
                self._skip(c, f"untouchable:#{n}")
                continue
            blk = view.block_of[n]
            unchanged = ((c.get("commit_sha") and c.get("commit_sha") == blk.get("commit_sha"))
                         or (c["source_type"] == "clawhub" and c.get("skill_tree_sha") == blk.get("skill_tree_sha"))
                         or (not c.get("commit_sha") and c["source_type"] == "github"
                             and (c.get("metrics") or {}).get("pushed_at") == (blk.get("metrics") or {}).get("pushed_at")))
            if unchanged:
                self._skip(c, "unchanged")
                continue
            if done >= self.caps["max_refresh"] or not self._reserve_ok():
                self._skip(c, "refresh_cap")
                continue
            c = {**c, "skill_path": blk.get("skill_path") or c.get("skill_path"),
                 "default_branch": c.get("default_branch") or blk.get("default_branch")}
            c["skill_tree_sha"] = c["skill_tree_sha"] if c["source_type"] == "clawhub" else None
            st = resolve_shas(self.client, c)
            if st != "ok":
                self._skip(c, f"refresh_{st}")
                continue
            done += 1
            fields = F.block_fields(c, blk.get("discovered_at") or F.iso())
            new_sha = F.block_sha(fields) != F.block_sha(blk)
            if F.same_block(blk, fields) and not new_sha:
                self._skip(c, "unchanged")
                continue
            block = F.build_block(fields)
            drop = F.review_labels(F.issue_labels(issue)) if new_sha else []
            self._act("refresh_block", c, n, new_sha=new_sha, labels_removed=drop,
                      old_sha=F.block_sha(blk), sha=F.block_sha(fields))
            if self.apply:
                self._write_block(n, issue, block, fields)
                for l in drop:
                    self.api.remove_label(n, l)
                self.index.put(c["identity"], issue=n, state="open", sha=F.block_sha(fields))

    def _write_block(self, n: int, issue: dict[str, Any], block: str, fields: dict[str, Any]) -> str:
        ie = self.index.identities.get(fields["psk-id"]) or {}
        if self.can_edit and ie.get("via") != "comment":
            try:
                self.api.edit_issue(n, body=F.replace_block(issue.get("body") or "", block))
                issue["body"] = F.replace_block(issue.get("body") or "", block)
                return "body"
            except (scout.Transient, scout.Unprocessable) as e:
                self.log(f"  body edit #{n} failed ({e}); using a marker comment")
        body = f"{MARKER_COMMENT_INTRO}\n\n{block}"
        cid = ie.get("comment_id")
        if not cid:
            for cm in self.api.list_comments(n):  # the LAST marker comment wins
                if F.parse_block(cm.get("body")):
                    cid = cm["id"]
        cid = self.api.edit_or_create_comment(n, cid, body)
        self.index.put(fields["psk-id"], issue=n, via="comment", comment_id=cid, block=fields)
        return "comment"

    def _do_legacy(self, items: list[tuple[dict[str, Any], int]], view: IssueView) -> None:
        def lprio(x: tuple[dict[str, Any], int]) -> tuple:
            c, n = x
            sub = c["identity"].split("::", 1)[1] if "::" in c["identity"] else ""
            return (c["status"] != "pass", -int((c.get("metrics") or {}).get("stars") or 0), n,
                    c["kind"] != "large-collection", sub.count("/") if sub else -1, c["identity"])
        items.sort(key=lprio)
        done = 0
        claimed: set[int] = set()
        for c, n in items:
            if n in claimed or n in view.block_of:
                self._skip(c, f"issue_already_claimed:#{n}")
                continue
            if done >= self.caps["max_legacy"]:
                self._skip(c, "legacy_cap")
                continue
            if not self._reserve_ok():
                self._skip(c, "core_reserve")
                continue
            st = resolve_shas(self.client, c)
            if st != "ok":
                self._skip(c, f"legacy_{st}")
                continue
            claimed.add(n)
            done += 1
            fields = F.block_fields(c, F.iso())
            block = F.build_block(fields)
            labels = [F.LABEL_CANDIDATE, F.LABEL_LEGACY, F.LABEL_FILED]
            if c["kind"] == "large-collection":
                labels.append(F.LABEL_LARGE)
            self._act("mark_legacy", c, n, via="body" if self.can_edit else "comment", labels=labels,
                      sha=F.block_sha(fields))
            if self.apply:
                issue = view.by_number[n]
                via = self._write_block(n, issue, block, fields)
                self.api.add_labels(n, labels)
                self.index.put(c["identity"], issue=n, state="open", kind="legacy", via=via, sha=F.block_sha(fields))
            view.block_of[n] = fields
            view.block_by_ident[c["identity"]] = n

    def _do_new(self, items: list[dict[str, Any]], view: IssueView, day: str) -> None:
        def prio(c: dict[str, Any]) -> tuple:
            m = c.get("metrics") or {}
            pop = int(m.get("stars") or 0) if c["source_type"] == "github" else int(m.get("downloads") or 0) // 10
            return (c["status"] != "pass", c["kind"] == "large-collection", -pop, c["identity"])
        items.sort(key=prio)
        created = 0
        filed_today = self.index.filed_today(day)
        per_repo = Counter(f.get("repo_key") for f in filed_today)
        total_today = len(filed_today)
        unreviewed = view.unreviewed_candidates
        for c in items:
            rk = F.repo_key_of(c["identity"])
            if unreviewed > self.caps["max_unreviewed"]:
                self.stopped = self.stopped or f"backpressure: {unreviewed} open candidate issues lack review:*"
                self._skip(c, "backpressure")
                continue
            if created >= self.caps["max_new"]:
                self._skip(c, "run_cap")
                continue
            if total_today >= self.caps["max_new_day"]:
                self._skip(c, "day_cap")
                continue
            if per_repo[rk] >= self.caps["max_new_repo_day"]:
                self._skip(c, "repo_day_cap")
                continue
            if not self._reserve_ok():
                self._skip(c, "core_reserve")
                continue
            st = resolve_shas(self.client, c)
            if st != "ok":
                self._skip(c, f"new_{st}")
                continue
            fields = F.block_fields(c, F.iso())
            block = F.build_block(fields)
            title = issue_title(c)
            body = issue_body(c, block)
            if not (F.no_handles(title) and F.no_handles(body)):
                self._skip(c, "handle_guard")
                continue
            labels = [F.LABEL_CANDIDATE, f"source:{c['source_type']}", F.LABEL_FILED]
            if c["kind"] == "large-collection":
                labels.append(F.LABEL_LARGE)
            num = None
            if self.apply:
                try:
                    num = int((self.api.create_issue(title, body, labels) or {}).get("number") or 0) or None
                except scout.GitHubError as e:
                    self.stopped = f"create failed: {type(e).__name__}: {e}"
                    self._skip(c, "create_failed")
                    break
                if num:
                    self.index.put(c["identity"], issue=num, state="open", kind="new", via="body",
                                   sha=F.block_sha(fields))
                    self.index.record_filed(num, c["identity"], rk, day)
                    self.index.save()  # never lose a created issue number
            self._act("create", c, num, title=title, labels=labels, sha=F.block_sha(fields),
                      stars=(c.get("metrics") or {}).get("stars"))
            created += 1
            total_today += 1
            per_repo[rk] += 1
            unreviewed += 1
            view.block_by_ident[c["identity"]] = num or -1


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="write to GitHub (default: dry-run)")
    ap.add_argument("--scout-queue", type=Path, default=None, help="default: today's/yesterday's candidate-queue")
    ap.add_argument("--source-candidates", type=Path, default=None,
                    help="default: today's/yesterday's source-candidates-YYYY-MM-DD.json")
    ap.add_argument("--no-scout", action="store_true")
    ap.add_argument("--no-sources", action="store_true")
    ap.add_argument("--source-type", choices=("github", "clawhub"), default=None, help="only this source type")
    ap.add_argument("--only", choices=("new", "legacy", "refresh"), default=None)
    ap.add_argument("--min-stars", type=int, default=0, help="GitHub candidates: minimum stars for NEW issues")
    for k, v in DEFAULT_CAPS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=int, default=v)
    ap.add_argument("--core-reserve", type=int, default=1500)
    ap.add_argument("--create-interval", type=float, default=3.0)
    ap.add_argument("--file-siblings", action="store_true")
    ap.add_argument("--no-closed-sync", action="store_true")
    ap.add_argument("--state-dir", type=Path, default=F.STATE_DIR)
    ap.add_argument("--ignore-routine-windows", action="store_true",
                    help="routine mode: skip the :10-:26 / :40-:58 Dhaka window check")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    args.create_interval = max(3.0, args.create_interval)

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
    cands: list[dict[str, Any]] = []
    if not args.no_scout:
        cands += load_scout_records(args.scout_queue or default_input("candidate-queue"))
    if not args.no_sources:
        cands += load_source_records(args.source_candidates or default_input("source-candidates"))
    if args.source_type:
        cands = [c for c in cands if c["source_type"] == args.source_type]
    if args.min_stars:
        cands = [c for c in cands if c.get("issue") or c["source_type"] != "github"
                 or int((c.get("metrics") or {}).get("stars") or 0) >= args.min_stars]
    caps = {k: getattr(args, k) for k in DEFAULT_CAPS}
    if args.only == "new":
        caps.update(max_legacy=0, max_refresh=0)
    elif args.only == "legacy":
        caps.update(max_new=0, max_refresh=0)
    elif args.only == "refresh":
        caps.update(max_new=0, max_legacy=0)
    client = scout.GitHubClient(scout.get_gh_token(), throttle=scout.AdaptiveThrottle(4))
    api = F.IssueRepo(client, core_reserve=args.core_reserve, create_interval=args.create_interval)
    index = IssueIndex(args.state_dir / INDEX_NAME)
    filer = ScoutFiler(api, index, apply=args.apply, caps=caps, file_siblings=args.file_siblings)
    print(f"[{scout.iso_now_dhaka()}] scout_file mode={'apply' if args.apply else 'dry-run'} candidates={len(cands)}",
          flush=True)
    try:
        out = filer.run(cands, sync_closed_issues=not args.no_closed_sync)
    except scout.GitHubError as e:
        print(f"ABORT: {type(e).__name__}: {e}", flush=True)
        index.save()
        return 2
    index.save()  # dry-run too: only the local mirror of what is on GitHub (+ closed-issue sync)
    out["summary"]["elapsed_s"] = round(time.time() - t0, 1)
    stamp = scout.now_dhaka().strftime("%Y-%m-%d-%H%M")
    path = args.out or ART / f"scout-file-{stamp}{'' if args.apply else '-dryrun'}.json"
    F.save_json(path, out)
    print(json.dumps(out["summary"], indent=2), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
