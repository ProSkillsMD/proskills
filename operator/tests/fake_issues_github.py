"""In-memory GitHub for the issue-flow tests: issues, comments, labels, repos, trees, raw files. No network."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import scout  # noqa: E402
from scout import AdaptiveThrottle, GitHubClient, Response  # noqa: E402

SLUG = scout.REPO_SLUG
SKILL = "---\nname: demo\ndescription: demo skill\n---\n# Demo\nDoes things safely.\n"


def _sha(*parts: str) -> str:
    return hashlib.sha1("\0".join(parts).encode()).hexdigest()


class FakeGH:
    def __init__(self, can_push: bool = True):
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, dict] = {}
        self.labels: set[str] = {"source:clawhub", "bug"}
        self.repos: dict[str, dict] = {}
        self.writes: list[tuple[str, str]] = []
        self.next_issue = 7000
        self.next_comment = 1
        self.can_push = can_push
        self.core_remaining = 4000
        self.forbidden_comments: set[int] = set()  # PATCH -> 403 (comment written by another account)

    # -- setup
    def add_repo(self, key: str, files: dict[str, str], *, spdx: str | None = "MIT", stars: int = 50,
                 branch: str = "main", archived: bool = False) -> None:
        self.repos[key.lower()] = {"key": key, "files": dict(files), "spdx": spdx, "stars": stars,
                                   "branch": branch, "archived": archived}

    def add_issue(self, number: int, title: str, body: str, labels=(), state: str = "open") -> dict:
        i = {"number": number, "title": title, "body": body, "state": state,
             "labels": [{"name": l} for l in labels], "created_at": "2026-09-01T00:00:00Z",
             "updated_at": "2026-09-01T00:00:00Z", "user": {"login": "someone"}}
        self.issues[number] = i
        return i

    def add_comment(self, number: int, body: str) -> dict:
        c = {"id": self.next_comment, "body": body, "issue": number}
        self.comments[self.next_comment] = c
        self.next_comment += 1
        return c

    def issue_comments(self, number: int) -> list[dict]:
        return [c for c in self.comments.values() if c["issue"] == number]

    def labels_of(self, number: int) -> set[str]:
        return {l["name"] for l in self.issues[number]["labels"]}

    # -- git objects
    def _dirs(self, r: dict) -> dict[str, str]:
        dirs: dict[str, str] = {}
        paths = sorted(r["files"])
        allds = {""}
        for p in paths:
            parts = p.split("/")
            for i in range(1, len(parts)):
                allds.add("/".join(parts[:i]))
        for d in allds:
            pref = d + "/" if d else ""
            content = "".join(f"{p}\0{r['files'][p]}" for p in paths if p.startswith(pref))
            dirs[d] = _sha("tree", content)
        return dirs

    def tree_sha(self, key: str, folder: str = "") -> str:
        return self._dirs(self.repos[key.lower()])[folder]

    def commit_sha(self, key: str) -> str:
        return _sha("commit", self.tree_sha(key))

    # -- transport
    def __call__(self, method, url, headers, body, timeout):
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        payload = json.loads(body) if body else None
        if u.hostname == "raw.githubusercontent.com":
            parts = u.path.lstrip("/").split("/", 3)
            owner, repo, ref, path = parts[0], parts[1], parts[2], unquote(parts[3])
            r = self.repos.get(f"{owner}/{repo}".lower())
            if r and path in r["files"] and ref in (r["branch"], self.commit_sha(r["key"]), "HEAD"):
                return self._r(200, text=r["files"][path])
            return self._r(404)
        path = unquote(u.path)
        if method != "GET":
            self.writes.append((method, path))
        pre = f"/repos/{SLUG}"
        if path == pre:
            return self._r(200, {"full_name": SLUG, "permissions": {"admin": self.can_push, "push": self.can_push,
                                                                   "pull": True}})
        if path.startswith(pre + "/"):
            return self._repo_api(method, path[len(pre):], q, payload)
        m = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", path)
        if m:
            return self._skill_repo(m.group(1), m.group(2), m.group(3) or "", q)
        return self._r(404)

    def _r(self, status=200, data=None, text=None):
        body = text.encode() if text is not None else (json.dumps(data).encode() if data is not None else b"")
        return Response(status, {"x-ratelimit-remaining": str(self.core_remaining), "x-ratelimit-resource": "core"}, body)

    def _repo_api(self, method, sub, q, payload):
        if sub == "/issues" and method == "GET":
            items = sorted(self.issues.values(), key=lambda i: i["number"])
            st = q.get("state", "open")
            if st != "all":
                items = [i for i in items if i["state"] == st]
            if q.get("labels"):
                want = set(q["labels"].split(","))
                items = [i for i in items if want <= {l["name"] for l in i["labels"]}]
            page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
            return self._r(200, items[(page - 1) * per: page * per])
        if sub == "/issues" and method == "POST":
            n = self.next_issue
            self.next_issue += 1
            for l in payload.get("labels") or []:
                if l not in self.labels:
                    return self._r(422, {"message": f"label {l} missing"})
            self.add_issue(n, payload["title"], payload["body"], payload.get("labels") or [])
            return self._r(201, self.issues[n])
        m = re.match(r"^/issues/(\d+)$", sub)
        if m:
            n = int(m.group(1))
            if n not in self.issues:
                return self._r(404)
            if method == "PATCH":
                self.issues[n].update(payload)
            return self._r(200, self.issues[n])
        m = re.match(r"^/issues/(\d+)/labels$", sub)
        if m and method == "POST":
            n = int(m.group(1))
            have = self.labels_of(n)
            for l in payload["labels"]:
                if l not in self.labels:
                    return self._r(422)
                have.add(l)
            self.issues[n]["labels"] = [{"name": l} for l in sorted(have)]
            return self._r(200, self.issues[n]["labels"])
        m = re.match(r"^/issues/(\d+)/labels/(.+)$", sub)
        if m and method == "DELETE":
            n, name = int(m.group(1)), m.group(2)
            have = self.labels_of(n)
            if name not in have:
                return self._r(404)
            self.issues[n]["labels"] = [{"name": l} for l in sorted(have - {name})]
            return self._r(200, [])
        m = re.match(r"^/issues/(\d+)/comments$", sub)
        if m:
            n = int(m.group(1))
            if method == "POST":
                return self._r(201, self.add_comment(n, payload["body"]))
            page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
            return self._r(200, self.issue_comments(n)[(page - 1) * per: page * per])
        m = re.match(r"^/issues/comments/(\d+)$", sub)
        if m:
            cid = int(m.group(1))
            if cid not in self.comments:
                return self._r(404)
            if method == "PATCH":
                if cid in self.forbidden_comments:
                    return self._r(403, {"message": "Resource not accessible by integration"})
                self.comments[cid]["body"] = payload["body"]
            return self._r(200, self.comments[cid])
        if sub == "/labels":
            if method == "POST":
                if payload["name"] in self.labels:
                    return self._r(422)
                self.labels.add(payload["name"])
                return self._r(201, payload)
            page = int(q.get("page", 1))
            items = [{"name": l} for l in sorted(self.labels)]
            return self._r(200, items[(page - 1) * 100: page * 100])
        return self._r(404)

    def _skill_repo(self, owner, repo, sub, q):
        r = self.repos.get(f"{owner}/{repo}".lower())
        if not r:
            return self._r(404)
        key = r["key"]
        if sub == "":
            return self._r(200, {"full_name": key, "default_branch": r["branch"], "archived": r["archived"],
                                 "stargazers_count": r["stars"],
                                 "license": {"spdx_id": r["spdx"], "key": (r["spdx"] or "").lower(), "name": r["spdx"]}
                                 if r["spdx"] else None})
        m = re.match(r"^/commits/(.+)$", sub)
        if m:
            return self._r(200, {"sha": self.commit_sha(key), "commit": {"tree": {"sha": self.tree_sha(key)}}})
        m = re.match(r"^/contents/?(.*)$", sub)
        if m:
            parent = m.group(1).strip("/")
            dirs = self._dirs(r)
            pref = parent + "/" if parent else ""
            names = {}
            for d, s in dirs.items():
                if d and d.startswith(pref) and "/" not in d[len(pref):]:
                    names[d[len(pref):]] = {"name": d[len(pref):], "type": "dir", "sha": s, "path": d}
            for p in r["files"]:
                if p.startswith(pref) and "/" not in p[len(pref):]:
                    names[p[len(pref):]] = {"name": p[len(pref):], "type": "file", "path": p}
            if parent and parent not in dirs:
                return self._r(404)
            return self._r(200, list(names.values()))
        m = re.match(r"^/git/trees/(.+)$", sub)
        if m:
            dirs = self._dirs(r)
            entries = [{"path": d, "type": "tree", "sha": s} for d, s in dirs.items() if d]
            entries += [{"path": p, "type": "blob", "sha": _sha("blob", t), "size": len(t)} for p, t in r["files"].items()]
            # like GitHub: the response "sha" echoes the resolved COMMIT when called with a branch/commit ref,
            # not the root tree sha (the root tree sha is only in commits/<ref> -> commit.tree.sha)
            return self._r(200, {"sha": self.commit_sha(key), "tree": entries, "truncated": False})
        return self._r(404)


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make_api(fake: FakeGH, clock: Clock | None = None, **kw):
    import issue_flow as F
    clock = clock or Clock()
    client = GitHubClient("t0k", transport=fake, throttle=AdaptiveThrottle(2), sleep=lambda s: None)
    return F.IssueRepo(client, sleep=clock.sleep, clock=clock, **kw), clock
