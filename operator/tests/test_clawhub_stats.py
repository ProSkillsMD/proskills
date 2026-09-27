"""clawhub_stats: catalog enrichment with a fake page fetcher. No network."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import clawhub_stats as S  # noqa: E402
from sources import clawhub as C  # noqa: E402

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def cat():
    return {"skills": [
        {"id": "a", "slug": "ai-coding-token-optimizer", "category": "coding", "author": "Asif2BD",
         "repo_url": "https://github.com/Asif2BD/AI-Coding-Token-Optimizer",
         "external_ratings": {"clawhub_url": "https://clawhub.ai/asif2bd/ai-coding-token-optimizer"}},
        {"id": "b", "slug": "brex", "category": "api", "author": "clawhub", "repo_url": "https://clawhub.ai/skills/brex",
         "external_ratings": {"clawhub_url": "https://clawhub.ai/skills/brex", "clawhub_downloads": 0, "clawhub_rating": 0}},
        {"id": "c", "slug": "getnote", "category": "productivity", "author": "iswalle",
         "repo_url": "https://github.com/iswalle/getnote-openclaw",
         "external_ratings": {"clawhub_url": "https://clawhub.ai/skills/getnote"}},
        {"id": "d", "slug": "plain", "category": "coding", "repo_url": "https://github.com/o/plain"},
    ]}


class Fake:
    def __init__(self, known):
        self.known, self.calls = known, []

    def __call__(self, owner, slug):
        self.calls.append((owner, slug))
        hit = self.known.get((owner.lower(), slug))
        return (owner.lower(), slug, dict(hit)) if hit else None


class ParseTests(unittest.TestCase):
    def test_url_forms(self):
        self.assertEqual(S.parse_clawhub_url("https://clawhub.ai/asif2bd/skills/x"), ("asif2bd", "x"))
        self.assertEqual(S.parse_clawhub_url("https://clawhub.ai/asif2bd/x"), ("asif2bd", "x"))
        self.assertEqual(S.parse_clawhub_url("https://clawhub.ai/@asif2bd/x"), ("asif2bd", "x"))
        self.assertEqual(S.parse_clawhub_url("https://clawhub.ai/skills/x"), (None, "x"))
        self.assertEqual(S.parse_clawhub_url("https://clawhub.ai/"), (None, None))

    def test_targets(self):
        c = cat()["skills"]
        self.assertEqual(S.target(c[0]), ("ai-coding-token-optimizer", ["asif2bd"]))
        self.assertEqual(S.target(c[1]), ("brex", []))  # "clawhub" author is not an owner
        self.assertEqual(S.target(c[2]), ("getnote", ["iswalle"]))
        self.assertFalse(S.is_clawhub_linked(c[3]))


class RefreshTests(unittest.TestCase):
    def test_append_only_and_canonical_url(self):
        c = cat()
        before = copy.deepcopy(c)
        f = Fake({("asif2bd", "ai-coding-token-optimizer"): {"downloads": 119, "installs": 2, "stars": 0, "comments": 0, "versions": 2},
                  ("iswalle", "getnote"): {"downloads": 30675, "installs": 766, "stars": 66, "comments": 0}})
        s = S.refresh(c, f, now=NOW)
        a, b, g, d = c["skills"]
        self.assertEqual(a["external_ratings"]["clawhub_downloads"], 119)
        self.assertEqual(a["external_ratings"]["clawhub_installs"], 2)
        self.assertEqual(a["external_ratings"]["clawhub_url"], "https://clawhub.ai/asif2bd/skills/ai-coding-token-optimizer")
        self.assertEqual(a["external_ratings"]["clawhub_stats_status"], "ok")
        self.assertEqual(g["external_ratings"]["clawhub_url"], "https://clawhub.ai/iswalle/skills/getnote")
        self.assertEqual(b["external_ratings"]["clawhub_stats_status"], "unresolved")
        self.assertEqual(b["external_ratings"]["clawhub_url"], "https://clawhub.ai/skills/brex")  # unresolved: untouched
        self.assertEqual(b["external_ratings"]["clawhub_downloads"], 0)
        self.assertEqual(d, before["skills"][3])
        for x, y in zip(c["skills"], before["skills"]):
            for k in ("id", "slug", "category", "repo_url"):
                self.assertEqual(x.get(k), y.get(k))
            self.assertEqual(set(y) - set(x), set())
        self.assertEqual(s["fetches"], 2)
        self.assertEqual(len(s["url_fixed"]), 2)

    def test_ttl_and_cap(self):
        c = cat()
        f = Fake({("asif2bd", "ai-coding-token-optimizer"): {"downloads": 1}, ("iswalle", "getnote"): {"downloads": 2}})
        S.refresh(c, f, now=NOW)
        f.calls.clear()
        S.refresh(c, f, now=NOW + timedelta(hours=1))
        self.assertEqual(f.calls, [])  # fresh
        S.refresh(c, f, now=NOW + timedelta(hours=25), max_fetches=1)
        self.assertEqual(len(f.calls), 1)  # capped
        f.calls.clear()
        S.refresh(c, f, now=NOW + timedelta(days=3))
        self.assertNotIn(("clawhub", "brex"), f.calls)  # unresolved retried only after 7x TTL

    def test_only_forces(self):
        c = cat()
        f = Fake({("asif2bd", "ai-coding-token-optimizer"): {"downloads": 5}})
        S.refresh(c, f, now=NOW)
        f.calls.clear()
        S.refresh(c, f, now=NOW, only=["ai-coding-token-optimizer"])
        self.assertEqual(f.calls, [("asif2bd", "ai-coding-token-optimizer")])


class PageStatsTests(unittest.TestCase):
    def test_uses_polite_client_and_canonical(self):
        html = ('<link rel="canonical" href="https://clawhub.ai/Asif2BD/skills/x"/>'
                '<script>stats:$R[5]={comments:0,downloads:9,installs:1,stars:3,versions:1}</script>')
        urls = []

        def fetch(url):
            urls.append(url)
            return 200, html

        cl = C.ClawHubClient(cache=None, fetch=fetch, sleep=lambda s: None)
        cl.rules = C.robots_rules("User-agent: *\nDisallow: /api/\n")
        self.assertEqual(S.PageStats(cl)("asif2bd", "x"), ("Asif2BD", "x", {"comments": 0, "downloads": 9, "installs": 1, "stars": 3, "versions": 1}))
        self.assertEqual(urls, ["https://clawhub.ai/asif2bd/skills/x"])

    def test_shell_page_is_not_found(self):
        cl = C.ClawHubClient(cache=None, fetch=lambda u: (200, "<html>shell</html>"), sleep=lambda s: None)
        cl.rules = []
        self.assertIsNone(S.PageStats(cl)("skills", "x"))


if __name__ == "__main__":
    unittest.main()
