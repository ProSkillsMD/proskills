"""Shared pieces of the issue-based scout -> review -> publish flow (stdlib only; no AI).

* Machine blocks (HTML comments, invisible when rendered):
    <!-- proskills:candidate v1          (issue body, or one marker comment on a legacy issue)
    psk-id: github:owner/repo::subpath
    repo: "https://github.com/owner/repo"
    ...
    -->
    <!-- proskills:review v1 psk-id=<identity> sha=<sha> verdict=<label> -->   (first line of the review comment)
  An identity containing '@' (clawhub:@owner/slug) is written with '%40' so no text we post can ever
  contain an @-handle.
* Label set of the new flow (created by issue_labels.py; old labels are never deleted or renamed).
* IssueRepo: thin REST wrapper over scout.GitHubClient with write pacing, a REST core reserve and
  no automatic retry on issue creation (a retried POST could open a duplicate).
* safe_text(): every piece of third-party text (skill names, descriptions) is neutralised before it is
  posted: no @-handles, no #123 / owner/repo#123 cross references, no raw URLs, no HTML, no table breaks.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

import scout
from publish_lib import BLOCKED_LABELS, PROTECTED_ISSUES, label_names

SCOUT_VERSION = "issue-flow/1"
REPO_SLUG = scout.REPO_SLUG
STATE_DIR = scout.SCOUT_STATE
DHAKA = scout.DHAKA

# Issues the new flow never touches (no marker, no label, no comment, no close).
CRITICAL_ISSUES = frozenset(scout.STATIC_HOLDS)          # 2396, 4869
PUBLISHER_SKIP_ISSUES = frozenset(scout.CREDENTIAL_LIKE_ISSUES)  # 2833
NEVER_TOUCH = frozenset(PROTECTED_ISSUES) | CRITICAL_ISSUES | PUBLISHER_SKIP_ISSUES
NEVER_TOUCH_LABELS = frozenset(BLOCKED_LABELS) | {"status:listed", "blocked:security"}
PUBLISHED_LABELS = frozenset({"groot:published", "status:listed", "status:published", "published",
                              "groot-published", "groot:done", "groot:merged"})

# --------------------------------------------------------------------------- labels

LABEL_CANDIDATE = "candidate"
LABEL_FILED = "scout:filed"
LABEL_LARGE = "scout:large-collection"
LABEL_LEGACY = "legacy:v0"
LABEL_AI_REVIEWED = "ai:reviewed"
LABEL_STAGED = "publish:staged"

VERDICTS = ("review:pass", "review:license-review", "review:hold-critical", "review:needs-ai",
            "review:reject", "review:large-collection", "review:hold-ai")
REJECT_REASONS = ("reject:no-skill-md", "reject:no-license", "reject:not-found", "reject:duplicate")

LABELS: dict[str, tuple[str, str]] = {
    LABEL_CANDIDATE: ("1d76db", "Skill candidate tracked by the ProSkills issue flow (machine block in body)"),
    LABEL_FILED: ("c5def5", "Filed or refreshed by scout_file.py (deterministic scout)"),
    LABEL_LARGE: ("fbca04", "Repo with more than 50 SKILL.md files: one issue for the whole collection"),
    LABEL_LEGACY: ("d4c5f9", "Opened by the old AI agent flow; machine block added by scout_file.py"),
    "review:pass": ("0e8a16", "Deterministic review passed: eligible for publishing at the reviewed sha"),
    "review:license-review": ("fef2c0", "License needs a human decision (no SPDX id; evidence recorded)"),
    "review:hold-critical": ("b60205", "Critical static-scan or platform security finding: held, never auto-published"),
    "review:needs-ai": ("bfdadc", "Deterministic checks inconclusive: queued for the budgeted AI review"),
    "review:reject": ("e99695", "Rejected by the deterministic review (see reject:* reason)"),
    "review:large-collection": ("fbca04", "Large collection: needs a human curation decision"),
    "review:hold-ai": ("d93f0b", "AI review did not approve: held for a human"),
    "reject:no-skill-md": ("f9d0c4", "No SKILL.md (with name and description) at the recorded path"),
    "reject:no-license": ("f9d0c4", "No license evidence"),
    "reject:not-found": ("f9d0c4", "Source repository or page not found on two checks at least 24h apart"),
    "reject:duplicate": ("f9d0c4", "Already listed in the ProSkills catalog (same identity)"),
    LABEL_AI_REVIEWED: ("5319e7", "Reviewed by the budgeted AI step (ai_review.py)"),
    LABEL_STAGED: ("0052cc", "Staged into a website catalog publish PR"),
    "operator-lock": ("000000", "Open issue with this label pauses the ProSkills GitHub Actions workflows"),
}
for _n in ("source:github", "source:clawhub"):
    LABELS.setdefault(_n, ("ededed", f"Candidate source: {_n.split(':', 1)[1]}"))

REVIEW_PREFIXES = ("review:", "reject:", "ai:")


def review_labels(labels: Iterable[str]) -> list[str]:
    return sorted(l for l in labels if l.startswith(REVIEW_PREFIXES))


def verdict_of(labels: Iterable[str]) -> str | None:
    vs = [l for l in labels if l in VERDICTS]
    return vs[0] if len(vs) == 1 else (sorted(vs)[0] if vs else None)


# --------------------------------------------------------------------------- time

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def dhaka_day(dt: datetime | None = None) -> str:
    return (dt or utc_now()).astimezone(DHAKA).date().isoformat()


# --------------------------------------------------------------------------- text safety

_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_XREF = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(?=\d)")
_WS = re.compile(r"\s+")


def safe_text(s: Any, limit: int = 300) -> str:
    """Neutralise third-party text for issue bodies/comments/titles.

    '@' -> U+FF20 (never a mention), '#123' / 'o/r#123' -> U+FF03 (never a cross reference),
    URLs -> '[link]', '<' '>' escaped, '|' escaped (tables), backticks dropped, one line, truncated.
    """
    t = _WS.sub(" ", str(s or "")).strip()
    t = _URL.sub("[link]", t)
    t = t.replace("@", "\uff20")
    t = _XREF.sub(lambda m: (m.group(1) or "") + "\uff03", t)
    t = t.replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("`", "'")
    if len(t) > limit:
        t = t[: max(0, limit - 1)].rstrip() + "\u2026"
    return t


def enc_id(identity: str) -> str:
    return str(identity or "").replace("%", "%25").replace("@", "%40")


def dec_id(value: str) -> str:
    return str(value or "").replace("%40", "@").replace("%25", "%")


def no_handles(text: str) -> bool:
    """True when `text` contains nothing GitHub would turn into a user/team mention."""
    return re.search(r"(?<![A-Za-z0-9_`/])@[A-Za-z0-9][A-Za-z0-9-]*", text or "") is None


# --------------------------------------------------------------------------- candidate block

CAND_RE = re.compile(r"<!-- proskills:candidate v1\n(.*?)\n-->", re.S)
REVIEW_RE = re.compile(r"<!-- proskills:review v1 psk-id=(\S+) sha=(\S+) verdict=(\S+) -->")
AI_START, AI_END = "<!-- proskills:ai v1 -->", "<!-- /proskills:ai -->"
BLOCK_KEYS = ("psk-id", "kind", "source_type", "repo", "source_url", "skill_path", "default_branch",
              "commit_sha", "skill_tree_sha", "license", "metrics", "sources", "skills_total",
              "discovered_at", "scout_version")


def _json_val(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    # ensure_ascii escapes non-ASCII; '@' and '-->' must never appear raw inside the comment
    return s.replace("@", "\\u0040").replace("-->", "--\\u003e")


def build_block(fields: dict[str, Any]) -> str:
    lines = ["<!-- proskills:candidate v1", f"psk-id: {enc_id(fields['psk-id'])}"]
    for k in BLOCK_KEYS[1:]:
        if k in fields:
            lines.append(f"{k}: {_json_val(fields[k])}")
    lines.append("-->")
    return "\n".join(lines)


def parse_block(text: str | None) -> dict[str, Any] | None:
    m = CAND_RE.search(text or "")
    if not m:
        return None
    out: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(": ")
        if not sep:
            continue
        k = k.strip()
        if k == "psk-id":
            out[k] = dec_id(v.strip())
            continue
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v.strip()
    return out if out.get("psk-id") else None


def replace_block(text: str, block: str) -> str:
    if CAND_RE.search(text or ""):
        return CAND_RE.sub(lambda _m: block, text, count=1)
    base = (text or "").rstrip()
    return (base + "\n\n" if base else "") + block


def block_sha(block: dict[str, Any] | None) -> str | None:
    if not block:
        return None
    return block.get("skill_tree_sha") or block.get("commit_sha") or None


def review_marker(identity: str, sha: str | None, verdict: str) -> str:
    return f"<!-- proskills:review v1 psk-id={enc_id(identity)} sha={sha or 'none'} verdict={verdict} -->"


def parse_review_marker(text: str | None) -> dict[str, str] | None:
    m = REVIEW_RE.search(text or "")
    if not m:
        return None
    return {"psk-id": dec_id(m.group(1)), "sha": m.group(2), "verdict": m.group(3)}


# --------------------------------------------------------------------------- records

def repo_key_of(identity: str) -> str:
    ident = (identity or "").lower()
    if ident.startswith("github:"):
        return ident[len("github:"):].split("::", 1)[0]
    if ident.startswith("clawhub:"):
        return "clawhub:" + ident[len("clawhub:"):].lstrip("@").split("/", 1)[0]
    return ident.split("::", 1)[0]


def display_path(rec: dict[str, Any]) -> str:
    """owner/repo[/subpath] (GitHub) or clawhub:owner/slug (no '@')."""
    ident = rec["identity"]
    if ident.startswith("clawhub:"):
        return "clawhub:" + ident[len("clawhub:"):].lstrip("@")
    rk = repo_key_of(ident)
    sub = ident.split("::", 1)[1] if "::" in ident else ""
    return rk + (f"/{sub}" if sub else "")


def normalize(rec: dict[str, Any], origin: str) -> dict[str, Any] | None:
    """Scout-queue or source-adapter record -> canonical candidate (None when unusable)."""
    ident = str(rec.get("identity") or "").strip()
    if not ident:
        return None
    ident = ident.lower() if ident.startswith("github:") else ident
    st = rec.get("source_type") or ("clawhub" if ident.startswith("clawhub:") else "github")
    large = rec.get("status") == "large_collection" or origin == "scout_large"
    lic_tier = rec.get("license_tier")
    lic = {"spdx": rec.get("license_spdx") or (rec.get("license") if lic_tier == "pass" else None),
           "label": rec.get("license"), "tier": lic_tier,
           "evidence": list(rec.get("license_evidence") or [])[:6]}
    if st == "clawhub":
        m = {}
        for s in rec.get("sources") or []:
            m.update(s.get("metrics") or {})
        metrics = {"downloads": m.get("clawhub_downloads"), "installs": m.get("clawhub_installs")
                   if m.get("clawhub_installs") is not None else m.get("clawhub_installsAllTime"),
                   "clawhub_stars": m.get("clawhub_stars"), "version": rec.get("version") or m.get("version")}
        tree_sha = "clawhub:" + str(rec.get("version") or "?") + (
            ":" + str(rec.get("integrity") or "").replace("sha256:", "")[:16] if rec.get("integrity") else "")
    else:
        metrics = {"stars": rec.get("stars"), "forks": rec.get("forks"), "pushed_at": rec.get("pushed_at")}
        tree_sha = rec.get("skill_tree_sha")
    sources = []
    for s in rec.get("sources") or []:
        sources.append({"source": s.get("source"), "url": s.get("source_url"), "observed_at": s.get("observed_at")})
    if origin.startswith("scout"):
        sources.append({"source": "issue", "url": None, "observed_at": rec.get("created_at")})
    owner = rec.get("owner")
    repo = rec.get("repo") or rec.get("slug")
    if not owner and ident.startswith("github:"):
        owner, _, repo = repo_key_of(ident).partition("/")
    return {
        "identity": ident, "kind": "large-collection" if large else "skill", "source_type": st,
        "origin": origin, "owner": owner, "repo": repo, "subpath": rec.get("subpath"),
        "repo_url": rec.get("repo_url") or (f"https://github.com/{repo_key_of(ident)}" if st == "github" else None),
        "source_url": rec.get("source_url") or rec.get("repo_url"),
        "skill_path": rec.get("skill_path") if not large else None,
        "default_branch": rec.get("default_branch"), "commit_sha": rec.get("commit_sha"),
        "skill_tree_sha": tree_sha, "license": lic, "metrics": metrics, "sources": sources[:6],
        "skills_total": rec.get("skills_total") or rec.get("repo_skills_total"),
        "name": rec.get("skill_name") or rec.get("name") or (rec.get("subpath") or "").rsplit("/", 1)[-1] or repo,
        "description": rec.get("skill_description") or rec.get("description"),
        "issue": rec.get("issue") or rec.get("existing_issue"), "status": rec.get("status"),
        "hold": rec.get("hold"), "score": rec.get("score"),
    }


def block_fields(c: dict[str, Any], discovered_at: str) -> dict[str, Any]:
    f = {"psk-id": c["identity"], "kind": c["kind"], "source_type": c["source_type"], "repo": c.get("repo_url"),
         "source_url": c.get("source_url"), "skill_path": c.get("skill_path"),
         "default_branch": c.get("default_branch"), "commit_sha": c.get("commit_sha"),
         "skill_tree_sha": c.get("skill_tree_sha"), "license": c.get("license"),
         "metrics": c.get("metrics"), "sources": c.get("sources"), "discovered_at": discovered_at,
         "scout_version": SCOUT_VERSION}
    if c["kind"] == "large-collection":
        f["skills_total"] = c.get("skills_total")
    return f


def same_block(a: dict[str, Any] | None, b: dict[str, Any]) -> bool:
    """Blocks equal ignoring discovered_at / observed_at churn."""
    if not a:
        return False
    def strip(x: dict[str, Any]) -> dict[str, Any]:
        y = {k: v for k, v in x.items() if k not in ("discovered_at", "sources", "metrics")}
        return json.loads(_json_val(y))
    return strip(a) == strip(b)


# --------------------------------------------------------------------------- issue helpers

def issue_labels(issue: dict[str, Any]) -> set[str]:
    return label_names(issue)


def untouchable(issue: dict[str, Any]) -> str | None:
    n = int(issue.get("number") or 0)
    if n in PROTECTED_ISSUES:
        return "protected_issue"
    if n in CRITICAL_ISSUES:
        return "critical_hold"
    if n in PUBLISHER_SKIP_ISSUES:
        return "publisher_skip"
    labs = issue_labels(issue)
    if labs & NEVER_TOUCH_LABELS:
        return "published_or_blocked:" + sorted(labs & NEVER_TOUCH_LABELS)[0]
    return None


class ReserveLow(Exception):
    pass


class IssueRepo:
    """Issue/label/comment REST calls with pacing. Reads go through scout.GitHubClient (retries etc.)."""

    def __init__(self, client: scout.GitHubClient, slug: str = REPO_SLUG, *, core_reserve: int = 1500,
                 create_interval: float = 3.0, write_interval: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.slug = slug
        self.core_reserve = core_reserve
        self.create_interval = create_interval
        self.write_interval = write_interval
        self.sleep = sleep
        self.clock = clock
        self._last_create: float | None = None
        self._last_write: float | None = None
        self.writes: list[dict[str, Any]] = []

    # -- plumbing
    def _url(self, path: str) -> str:
        return f"{scout.API}/repos/{self.slug}/{path.lstrip('/')}"

    def check_reserve(self) -> None:
        cr = self.client.core_remaining
        if cr is not None and cr < self.core_reserve:
            raise ReserveLow(f"REST core remaining {cr} < reserve {self.core_reserve}")

    def _pace(self, create: bool) -> None:
        now = self.clock()
        gaps = [(self._last_write, self.write_interval)]
        if create:
            gaps.append((self._last_create, self.create_interval))
        wait = max([iv - (now - last) for last, iv in gaps if last is not None] + [0])
        if wait > 0:
            self.sleep(wait)
        self._last_write = self.clock()
        if create:
            self._last_create = self._last_write

    def _write(self, method: str, path: str, payload: Any | None, *, create: bool = False) -> Any:
        self.check_reserve()
        self._pace(create)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        saved = self.client.max_retries
        if create:
            self.client.max_retries = 0  # never retry a POST that opens an issue/comment
        try:
            resp = self.client.request(self._url(path), method=method, body=body)
        finally:
            self.client.max_retries = saved
        self.writes.append({"method": method, "path": path})
        return resp.json() if resp.body else None

    def get(self, path: str) -> Any:
        return self.client.request(self._url(path)).json()

    # -- reads
    def list_issues(self, *, state: str = "open", labels: str | None = None, since: str | None = None,
                    max_pages: int = 80) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        q = f"state={state}&per_page=100&sort=created&direction=asc"
        if labels:
            q += f"&labels={quote(labels, safe=',')}"
        if since:
            q += f"&since={quote(since)}"
        for page in range(1, max_pages + 1):
            items = self.get(f"issues?{q}&page={page}") or []
            out.extend(i for i in items if "pull_request" not in i)
            if len(items) < 100:
                break
        return out

    def get_issue(self, number: int) -> dict[str, Any]:
        return self.get(f"issues/{int(number)}")

    def list_comments(self, number: int, max_pages: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            items = self.get(f"issues/{int(number)}/comments?per_page=100&page={page}") or []
            out.extend(items)
            if len(items) < 100:
                break
        return out

    def can_edit_bodies(self) -> bool:
        # GitHub App installation tokens get no `permissions` block on GET /repos; the Actions workflows
        # declare the app's issues:write grant explicitly.
        if os.environ.get("PROSKILLS_ISSUES_WRITE", "").lower() == "true":
            return True
        try:
            perms = (self.client.request(f"{scout.API}/repos/{self.slug}").json() or {}).get("permissions") or {}
        except scout.GitHubError:
            return False
        return bool(perms.get("push") or perms.get("maintain") or perms.get("admin"))

    def label_set(self) -> set[str]:
        names: set[str] = set()
        for page in range(1, 20):
            items = self.get(f"labels?per_page=100&page={page}") or []
            names |= {i.get("name") for i in items if i.get("name")}
            if len(items) < 100:
                break
        return names

    # -- writes
    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self._write("POST", "issues", {"title": title, "body": body, "labels": labels}, create=True)

    def edit_issue(self, number: int, **fields: Any) -> dict[str, Any]:
        return self._write("PATCH", f"issues/{int(number)}", fields)

    def add_labels(self, number: int, labels: Iterable[str]) -> None:
        labs = sorted(set(labels))
        if labs:
            self._write("POST", f"issues/{int(number)}/labels", {"labels": labs})

    def remove_label(self, number: int, name: str) -> None:
        try:
            self._write("DELETE", f"issues/{int(number)}/labels/{quote(name, safe='')}", None)
        except scout.NotFound:
            pass

    def create_comment(self, number: int, body: str) -> dict[str, Any]:
        return self._write("POST", f"issues/{int(number)}/comments", {"body": body}, create=True)

    def edit_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        return self._write("PATCH", f"issues/comments/{int(comment_id)}", {"body": body})

    def edit_or_create_comment(self, number: int, comment_id: int | None, body: str) -> int | None:
        """Edit our marker comment in place; if it is gone (404) or not editable by this identity (403, e.g.
        a comment written by the box account before the GitHub Actions cutover) post a fresh one, which then
        becomes the LAST marker comment and is the one found next time."""
        if comment_id:
            try:
                self.edit_comment(int(comment_id), body)
                return int(comment_id)
            except scout.NotFound:
                pass
            except scout.Transient as e:
                if getattr(e, "status", None) != 403:
                    raise
        return (self.create_comment(number, body) or {}).get("id")

    def close_not_planned(self, number: int) -> dict[str, Any]:
        return self._write("PATCH", f"issues/{int(number)}", {"state": "closed", "state_reason": "not_planned"})

    def create_label(self, name: str, color: str, description: str) -> None:
        self._write("POST", "labels", {"name": name, "color": color, "description": description[:100]})


def set_labels(api: IssueRepo, issue: dict[str, Any], want: Iterable[str], drop_prefixes: tuple[str, ...]) -> dict[str, list[str]]:
    """Make the issue carry `want` and none of the other labels that start with `drop_prefixes`."""
    have = issue_labels(issue)
    want_s = set(want)
    add = sorted(want_s - have)
    remove = sorted(l for l in have if l.startswith(drop_prefixes) and l not in want_s)
    for l in remove:
        api.remove_label(issue["number"], l)
    if add:
        api.add_labels(issue["number"], add)
    issue["labels"] = [{"name": l} for l in sorted((have - set(remove)) | want_s)]
    return {"add": add, "remove": remove}


# --------------------------------------------------------------------------- json state

def load_json(path: Path, default: Any) -> Any:
    return scout._load_json(path, default)


def save_json(path: Path, data: Any) -> None:
    scout._atomic_write(path, data)


def live_routine_lock(locks_dir: Path | None = None) -> dict[str, Any] | None:
    """A live hourly-routine lock in operator/state/locks (pid alive) -> its info, else None."""
    d = locks_dir or scout.LOCKS
    for p in d.glob("*.lock") if d.exists() else []:
        try:
            raw = p.read_text(encoding="utf-8").strip()
            data = json.loads(raw) if raw.startswith("{") else {"pid": int(raw) if raw.isdigit() else None}
        except Exception:
            continue
        pid = data.get("pid")
        if pid and Path(f"/proc/{pid}").exists():
            return {"lock": p.name, "routine": data.get("routine"), "pid": pid}
    return None


def in_routine_window(now: datetime | None = None) -> bool:
    """Dhaka minutes :10-:26 (intake at :14) and :40-:58 (publisher at :44)."""
    m = (now or utc_now()).astimezone(DHAKA).minute
    return 10 <= m <= 26 or 40 <= m <= 58
