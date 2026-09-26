"""SkillsMP adapter with a fake MCP server. No network."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scout  # noqa: E402
import scout_file  # noqa: E402
import source_candidates  # noqa: E402
from sources import skillsmp  # noqa: E402
from sources.base import DiskCache  # noqa: E402
from test_scout import SKILL, _no_network, client_for  # noqa: E402
from test_sources import SearchFake  # noqa: E402

ROBOTS = ("User-Agent: *\nAllow: /\nAllow: /api/llms.txt\nDisallow: /api/github-contents\nDisallow: /api/\n"
          "Disallow: /auth/\nCrawl-delay: 1\n")


def listing(i, gh, stars=5):
    return {"id": i, "name": i.split("-")[0], "author": "acme", "description": "d", "contentLanguage": "en",
            "githubUrl": gh, "skillUrl": f"https://skillsmp.com/creators/acme/{i}", "stars": stars,
            "updatedAt": 1790312599}


PAGES = {
    ("skill", "recent", 1): ([listing("alpha-skill-md", "https://github.com/NV/skills/tree/main/skills/alpha"),
                              listing("listed-skill-md", "https://github.com/Old/listed/tree/main/skills/x"),
                              listing("orphan-skill-md", None, stars=0)], True),
    ("skill", "recent", 2): ([listing("beta-skill-md", "https://github.com/NV/skills/blob/main/skills/beta/SKILL.md"),
                              listing("alpha-skill-md", "https://github.com/NV/skills/tree/main/skills/alpha")], True),
    ("skill", "recent", 3): ([listing("gamma-skill-md", "https://github.com/NV/skills/tree/main/skills/gamma")], False),
    ("agent", "stars", 1): ([listing("root-skill-md", "https://github.com/Solo/one/tree/main", stars=900)], False),
}


class FakeMCP:
    def __init__(self, robots=ROBOTS, fail=None):
        self.robots = robots
        self.fail = list(fail or [])  # queued (status, headers) for tools/call
        self.posts: list[dict] = []
        self.gets: list[str] = []
        self.headers: list[dict] = []

    def get(self, url):
        self.gets.append(url)
        assert url == "https://skillsmp.com/robots.txt", url
        return (200, self.robots) if self.robots is not None else (404, "")

    def post(self, url, body, headers):
        assert url == "https://skillsmp.com/mcp", url
        assert "/api/" not in url
        msg = json.loads(body)
        self.posts.append(msg)
        self.headers.append(headers)
        m = msg["method"]
        if m == "initialize":
            return 200, {"mcp-session-id": "s1"}, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": skillsmp.PROTOCOL_VERSION, "capabilities": {"tools": {}}}})
        if m == "notifications/initialized":
            return 202, {}, ""
        assert m == "tools/call" and msg["params"]["name"] == "search_skills"
        if self.fail:
            st, h = self.fail.pop(0)
            return st, h, ""
        a = msg["params"]["arguments"]
        skills, more = PAGES.get((a["query"], a["sortBy"], a["page"]), ([], False))
        text = json.dumps({"skills": skills, "pagination": {"page": a["page"], "hasNext": more}})
        # answer as SSE once to exercise that path
        if a["query"] == "agent":
            return 200, {"content-type": "text/event-stream"}, (
                "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                                       "result": {"content": [{"type": "text", "text": text}]}}) + "\n\n")
        return 200, {}, json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                    "result": {"content": [{"type": "text", "text": text}]}})

    def calls(self):
        return [p for p in self.posts if p["method"] == "tools/call"]


CATALOG = {"skills": [{"repo_url": "https://github.com/old/listed", "path": "skills/x"}]}
QUERIES = [{"q": "skill", "sortBy": "recent"}, {"q": "agent", "sortBy": "stars"}]


class Base(unittest.TestCase):
    def setUp(self):
        for t in (mock.patch.object(scout, "urllib_transport", _no_network),
                  mock.patch("urllib.request.urlopen", _no_network)):
            t.start()
            self.addCleanup(t.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sleeps: list[float] = []

    def client(self, site, name="c.json", t=None):
        t = t or [0.0]
        return skillsmp.SkillsMPClient(DiskCache(self.root / name), post=site.post, get=site.get,
                                       sleep=lambda s: (self.sleeps.append(s), t.__setitem__(0, t[0] + s)),
                                       clock=lambda: t[0])


class TestClient(Base):
    def test_never_calls_rest_api_and_respects_robots(self):
        site = FakeMCP()
        c = self.client(site)
        rules = c.load_robots()
        self.assertTrue(c.mcp_allowed())
        self.assertFalse(skillsmp.robots_allows(rules, "/api/v1/skills/search"))
        # robots disallowing /mcp -> nothing is called
        site2 = FakeMCP(robots="User-agent: *\nDisallow: /\n")
        obs, recs, info = skillsmp.collect(self.client(site2, "d.json"), CATALOG, QUERIES)
        self.assertEqual((obs, recs, site2.posts), ([], [], []))
        self.assertEqual(info["error"], "mcp_disallowed_by_robots")

    def test_protocol_headers_and_session(self):
        site = FakeMCP()
        c = self.client(site)
        c.load_robots()
        c.search("skill", sort="recent")
        self.assertEqual([p["method"] for p in site.posts], ["initialize", "notifications/initialized", "tools/call"])
        self.assertNotIn("id", site.posts[1])
        h = site.headers[-1]
        self.assertEqual(h["MCP-Protocol-Version"], skillsmp.PROTOCOL_VERSION)
        self.assertEqual(h["Mcp-Session-Id"], "s1")
        self.assertIn("proskills-operator", h["User-Agent"])
        self.assertNotIn("Authorization", h)  # no key, no account
        self.assertEqual(site.posts[-1]["params"]["arguments"]["limit"], 50)
        self.assertTrue(all(s >= 2.5 - 1e-9 for s in self.sleeps))  # spaced >= 2.5 s

    def test_429_retry_after_then_success(self):
        site = FakeMCP(fail=[(429, {"retry-after": "7"})])
        c = self.client(site)
        c.load_robots()
        data = c.search("skill", sort="recent")
        self.assertEqual(len(data["skills"]), 3)
        self.assertIn(7.0, self.sleeps)

    def test_transient_is_not_cached_and_stops_run(self):
        site = FakeMCP(fail=[(503, {}), (503, {})])
        c = self.client(site)
        obs, recs, info = skillsmp.collect(c, CATALOG, QUERIES, max_calls=10)
        self.assertEqual(obs, [])
        self.assertTrue(info["error"].startswith("transient:"))
        self.assertEqual(len(site.calls()), 2)  # one retry, then stop: no further queries this run
        self.assertGreaterEqual(info["pages_deferred"], 2)
        self.assertIsNone(c.search("skill", sort="recent", allow_fetch=False))  # failure never cached
        # next run: retried and succeeds
        obs2, _, info2 = skillsmp.collect(self.client(site, "c.json"), CATALOG, QUERIES, max_calls=10)
        self.assertNotIn("error", info2)
        self.assertTrue(obs2)

    def test_retry_after_too_long_is_transient(self):
        site = FakeMCP(fail=[(429, {"retry-after": "3600"})])
        c = self.client(site)
        c.load_robots()
        with self.assertRaises(skillsmp.SkillsMPTransient):
            c.search("skill")
        self.assertNotIn(3600.0, self.sleeps)

    def test_permanent_error_skips_query_only(self):
        site = FakeMCP(fail=[(400, {})])
        obs, _, info = skillsmp.collect(self.client(site), CATALOG, QUERIES, max_calls=10)
        self.assertEqual(len(info["errors"]), 1)
        self.assertNotIn("error", info)
        self.assertEqual([(o["owner"], o["repo"]) for o in obs], [("solo", "one")])


class TestCollect(Base):
    def test_mapping_dedupe_and_skillsmp_only(self):
        site = FakeMCP()
        obs, recs, info = skillsmp.collect(self.client(site), CATALOG, QUERIES, max_calls=10, max_pages=5)
        by = {(o["owner"], o["repo"], o["subpath"]) for o in obs}
        self.assertEqual(by, {("nv", "skills", "skills/alpha"), ("nv", "skills", "skills/beta"),
                              ("nv", "skills", "skills/gamma"), ("old", "listed", "skills/x"),
                              ("solo", "one", None)})
        self.assertEqual(info["listings_seen"], 6)
        self.assertEqual(info["duplicate_listings"], 1)
        self.assertEqual(info["github_backed_listings"], 5)
        self.assertEqual(info["skillsmp_only_listings"], 1)
        o = next(o for o in obs if o["subpath"] == "skills/alpha")
        self.assertEqual(o["source"], "skillsmp")
        self.assertEqual(o["source_url"], "https://skillsmp.com/creators/acme/alpha-skill-md")
        self.assertEqual(o["metrics"]["mapping"], "githubUrl")
        self.assertEqual(o["metrics"]["skillsmp_id"], "alpha-skill-md")
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["identity"], "skillsmp:orphan-skill-md")
        self.assertEqual(r["source_type"], "skillsmp")
        self.assertEqual(r["status"], "missing_license")
        self.assertIsNone(r["license_tier"])
        self.assertEqual(info["api_allowed"], False)
        self.assertEqual(site.gets, ["https://skillsmp.com/robots.txt"])

    def test_bounded_calls_cache_and_rotation(self):
        site = FakeMCP()
        c = self.client(site)
        _, _, info = skillsmp.collect(c, CATALOG, QUERIES, max_calls=2, max_pages=5)
        self.assertEqual(info["calls"], 2)
        self.assertEqual(len(site.calls()), 2)
        self.assertEqual(info["pages_deferred"], 2)  # skill p3 + agent p1
        c.cache.save()
        site.posts.clear()
        site.gets.clear()
        # next run, same cache: the two pages are free; only two more uncached calls allowed
        c2 = self.client(site)
        _, _, info2 = skillsmp.collect(c2, CATALOG, QUERIES, max_calls=2, max_pages=5)
        self.assertEqual(info2["pages_cached"], 2)
        self.assertEqual(len(site.calls()), 2)
        self.assertEqual(info2["pages_deferred"], 0)
        self.assertEqual(site.gets, [])  # robots cached 24 h
        # rotation puts the second query first
        site3 = FakeMCP()
        skillsmp.collect(self.client(site3, "r.json"), CATALOG, QUERIES, max_calls=1, rotation=1)
        self.assertEqual(site3.calls()[0]["params"]["arguments"]["query"], "agent")

    def test_idempotent(self):
        site = FakeMCP()
        c = self.client(site)
        a = skillsmp.collect(c, CATALOG, QUERIES, max_calls=10, max_pages=5, now_iso="2026-09-26T00:00:00Z")
        n = len(site.calls())
        b = skillsmp.collect(c, CATALOG, QUERIES, max_calls=10, max_pages=5, now_iso="2026-09-26T00:00:00Z")
        self.assertEqual(a[:2], b[:2])
        self.assertEqual(len(site.calls()), n)  # second run served from cache

    def test_min_stars(self):
        site = FakeMCP()
        obs, recs, info = skillsmp.collect(self.client(site), CATALOG, QUERIES, max_calls=10, max_pages=5,
                                           min_stars=100)
        self.assertEqual([(o["owner"], o["repo"]) for o in obs], [("solo", "one")])
        self.assertEqual(recs, [])
        self.assertEqual(info["below_min_stars"], 5)


class TestEndToEnd(Base):
    def run_sources(self, site, g, issue_index=None):
        cfg = json.loads((ROOT / "config" / "sources.json").read_text())
        cfg["skillsmp"]["queries"] = QUERIES
        cfg["skillsmp"]["max_pages"] = 5
        cfg_path = self.root / "s.json"
        cfg_path.write_text(json.dumps(cfg))
        art = self.root / "art"
        art.mkdir(exist_ok=True)
        idx = self.root / "issue-index.json"
        idx.write_text(json.dumps({"identities": issue_index or {}}))
        args = argparse.Namespace(sources="skillsmp,awesome", config=cfg_path, max_tree_fetches=None, max_scan=None,
                                  no_scan=False, no_persist_state=False, no_scout_cache=True, catalog=None,
                                  skillsmp_max_calls=10)
        return source_candidates.run(args, client=client_for(g), catalog=CATALOG, state_dir=self.root / "state",
                                     art=art, now=datetime(2026, 9, 26, 6, tzinfo=timezone.utc),
                                     sleep=lambda s: None, skillsmp_post=site.post, skillsmp_get=site.get,
                                     issue_index_path=idx)

    def test_run_normalises_to_github_identity(self):
        g = SearchFake()
        g.add_repo("NV/skills", files={"skills/alpha/SKILL.md": SKILL, "skills/beta/SKILL.md": SKILL,
                                       "skills/gamma/SKILL.md": SKILL, "skills/other/SKILL.md": SKILL}, stars=300)
        g.add_repo("Old/listed", files={"skills/x/SKILL.md": SKILL}, stars=50)
        g.add_repo("Solo/one", files={"SKILL.md": SKILL}, stars=900)
        # awesome list also points at alpha -> one identity, two provenance entries
        g.add_repo("VoltAgent/awesome-agent-skills", files={"README.md": "- https://github.com/NV/skills/tree/main/skills/alpha\n"})
        site = FakeMCP()
        out = self.run_sources(site, g, issue_index={"github:nv/skills::skills/gamma": {"issue": 6200}})
        by = {r["identity"]: r for r in out["candidates"]}
        alpha = by["github:nv/skills::skills/alpha"]
        self.assertEqual(alpha["status"], "pass")
        self.assertEqual(alpha["source_type"], "github")
        self.assertEqual({s["source"] for s in alpha["sources"]}, {"skillsmp", "awesome:voltagent/awesome-agent-skills"})
        self.assertNotIn("github:nv/skills::skills/other", by)  # mapped to the listed folders only
        self.assertEqual(by["github:old/listed::skills/x"]["status"], "already_in_catalog")
        self.assertIn("github:solo/one", by)
        self.assertEqual(by["skillsmp:orphan-skill-md"]["status"], "missing_license")
        bd = out["summary"]["source_breakdown"]["skillsmp"]
        self.assertEqual(bd["listings_seen"], 6)
        self.assertEqual(bd["github_backed_listings"], 5)
        self.assertEqual(bd["skillsmp_only_listings"], 1)
        self.assertEqual(bd["identities"], 6)
        self.assertEqual(bd["github_backed"], 5)
        self.assertEqual(bd["skillsmp_only"], 1)
        self.assertEqual(bd["known_in_catalog"], 1)
        self.assertEqual(bd["known_issue"], 1)
        self.assertEqual(bd["new"], 4)
        self.assertEqual(bd["new_fileable"], 3)  # alpha, beta, solo (orphan is never fileable)
        self.assertEqual(bd["also_seen_via_other_sources"], 1)
        self.assertEqual(list((self.root / "art").glob("candidate-queue-*")), [])
        # the filer takes the GitHub-backed records only; the skillsmp-only one is never filed
        p = self.root / "sc.json"
        p.write_text(json.dumps({"candidates": out["candidates"] + [{**by["skillsmp:orphan-skill-md"], "status": "pass"}]}))
        filed = {c["identity"] for c in scout_file.load_source_records(p)}
        self.assertIn("github:nv/skills::skills/alpha", filed)
        self.assertFalse(any(i.startswith("skillsmp:") for i in filed))

    def test_renamed_repo_listed_twice_is_one_identity(self):
        a = {"identity": "github:prisma/orm::x", "status": "pass",
             "sources": [{"source": "skillsmp", "source_url": "https://skillsmp.com/creators/prisma/orm/x"}]}
        b = {"identity": "github:prisma/orm::x", "status": "pass",
             "sources": [{"source": "skillsmp", "source_url": "https://skillsmp.com/creators/prisma/prisma/x"}]}
        out = source_candidates.merge_duplicate_identities([a, b, dict(a, identity="github:o/r")])
        self.assertEqual([r["identity"] for r in out], ["github:prisma/orm::x", "github:o/r"])
        self.assertEqual(len(out[0]["sources"]), 2)

    def test_skillsmp_in_default_sources(self):
        self.assertIn("skillsmp", source_candidates.ALL_SOURCES)


if __name__ == "__main__":
    unittest.main()
