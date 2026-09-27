"""clawhub_stats: documented-API catalog enrichment with a fake HTTP layer. No network."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import clawhub_stats as S  # noqa: E402
from sources.base import DiskCache  # noqa: E402

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
T0 = "2026-09-20T10:00:00Z"


def skill_body(owner, downloads=119, installs=2, stars=0, comments=0):
    return json.dumps({"skill": {"slug": "x", "stats": {"comments": comments, "downloads": downloads,
                                                        "installs": installs, "stars": stars, "versions": 2}},
                       "owner": {"handle": owner}})


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class FakeHTTP:
    """routes: path(+query) -> list of (status, headers, body) served in order (last one repeats)."""

    def __init__(self, routes, clock=None, latency=0.0):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []
        self.clock, self.latency = clock, latency

    def __call__(self, url):
        key = url.replace(S.BASE, "")
        self.calls.append(key)
        if self.clock is not None:
            self.clock.t += self.latency
        seq = self.routes.get(key)
        if not seq:
            return 404, {}, "Skill not found"
        return seq.pop(0) if len(seq) > 1 else seq[0]


def client(routes, *, cache=None, **kw):
    clock = Clock()
    http = FakeHTTP(routes, clock, kw.pop("latency", 0.0))
    c = S.ApiClient(cache, fetch=http, sleep=clock.sleep, clock=clock, rand=lambda: 0.5, **kw)
    return c, http, clock


def cat():
    return {"skills": [
        {"id": "a", "slug": "ai-coding-token-optimizer", "category": "coding", "author": "Asif2BD",
         "repo_url": "https://github.com/Asif2BD/AI-Coding-Token-Optimizer",
         "external_ratings": {"clawhub_url": "https://clawhub.ai/asif2bd/ai-coding-token-optimizer"}},
        {"id": "b", "slug": "brex", "category": "api", "author": "clawhub", "repo_url": "https://clawhub.ai/skills/brex",
         "external_ratings": {"clawhub_url": "https://clawhub.ai/skills/brex", "clawhub_downloads": 57}},
        {"id": "c", "slug": "gog-listing", "category": "productivity", "repo_url": "https://github.com/o/gog",
         "external_ratings": {"clawhub_url": "https://clawhub.ai/thcjp/skills/gog"}},
        {"id": "d", "slug": "plain", "category": "coding", "repo_url": "https://github.com/o/plain"},
    ]}


def last_good(er, downloads=119):
    er.update(clawhub_downloads=downloads, clawhub_installs=2, clawhub_stats_status="ok", clawhub_last_success_at=T0,
              clawhub_fetched_at=T0, clawhub_url="https://clawhub.ai/asif2bd/skills/ai-coding-token-optimizer")


class ParseTests(unittest.TestCase):
    def test_url_forms(self):
        self.assertEqual(S.parse_clawhub_url("https://clawhub.ai/asif2bd/skills/x"), ("asif2bd", "x"))
        self.assertEqual(S.parse_clawhub_url("https://clawhub.ai/asif2bd/x"), ("asif2bd", "x"))
        self.assertEqual(S.parse_clawhub_url("https://clawhub.ai/@asif2bd/x"), ("asif2bd", "x"))
        self.assertEqual(S.parse_clawhub_url("https://clawhub.ai/skills/x"), (None, "x"))
        self.assertEqual(S.parse_clawhub_url("https://clawhub.ai/"), (None, None))

    def test_target_trusts_only_clawhub_urls(self):
        c = cat()["skills"]
        self.assertEqual(S.target(c[0]), ("ai-coding-token-optimizer", "asif2bd"))
        self.assertEqual(S.target(c[1]), ("brex", None))
        self.assertFalse(S.is_clawhub_linked(c[3]))


class ClientTests(unittest.TestCase):
    def test_only_documented_read_endpoints(self):
        c, _, _ = client({})
        with self.assertRaises(PermissionError):
            c._get("/api/v1/skills/x/file")
        with self.assertRaises(PermissionError):
            c._get("/asif2bd/skills/x")

    def test_retries_with_exponential_backoff_and_jitter_then_fetch_failed(self):
        c, http, _ = client({"/api/v1/skills/x": [(503, {}, "")]}, max_attempts=3, backoff_base=2.0)
        r = c.skill("x", None)
        self.assertEqual((r.status, r.reason), ("fetch_failed", "http_503"))
        self.assertEqual(len(http.calls), 3)
        # rand=0.5 -> equal jitter midpoint: 0.75 * 2^(n-1) * base
        self.assertEqual(c.sleeps, [1.5, 3.0])

    def test_jitter_bounds(self):
        c, _, _ = client({})
        for n in (1, 2, 3, 6):
            for rv in (0.0, 1.0):
                c.rand = lambda rv=rv: rv
                exp = min(c.backoff_cap, c.backoff_base * 2 ** (n - 1))
                self.assertTrue(exp / 2 <= c.backoff(n, None) <= exp)

    def test_retry_after_delta_seconds(self):
        c, http, _ = client({"/api/v1/skills/x": [(429, {"retry-after": "7"}, ""), (503, {"retry-after": "12"}, ""),
                                                   (200, {}, skill_body("o"))]}, retry_after_cap=60)
        self.assertEqual(c.skill("x", None).status, "ok")
        self.assertEqual(c.sleeps, [7.0, 12.0])
        self.assertEqual(c.attempt_times[1] - c.attempt_times[0], 7.0)

    def test_retry_after_http_date(self):
        wall = datetime(2026, 9, 27, 14, 40, 0, tzinfo=timezone.utc)
        c, http, _ = client({"/api/v1/skills/x": [(429, {"retry-after": "Sun, 27 Sep 2026 14:40:09 GMT"}, ""),
                                                   (200, {}, skill_body("o"))]}, wallclock=lambda: wall)
        self.assertEqual(c.retry_after_seconds("Sun, 27 Sep 2026 14:40:09 GMT"), 9.0)
        self.assertEqual(c.retry_after_seconds("Sun, 27 Sep 2026 14:39:00 GMT"), 0.0)  # past date -> no wait
        self.assertIsNone(c.retry_after_seconds("soon"))
        self.assertEqual(c.skill("x", None).status, "ok")
        self.assertEqual(c.sleeps, [9.0])

    def test_retry_after_beyond_cap_halts_run_without_sleeping(self):
        routes = {"/api/v1/skills/x": [(429, {"retry-after": "120"}, "")], "/api/v1/skills/y": [(200, {}, skill_body("o"))]}
        c, http, _ = client(routes, retry_after_cap=60)
        r = c.skill("x", None)
        self.assertEqual((r.status, r.reason), ("fetch_failed", "http_429"))
        self.assertEqual(c.sleeps, [])          # did not sleep 120 s, did not retry early
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(c.halted, "retry_after_120s")
        self.assertEqual(c.skill("y", None).status, "deferred")
        self.assertEqual(len(http.calls), 1)

    def test_final_attempt_retry_after_halts_run_without_next_skill_request(self):
        routes = {"/api/v1/skills/x": [(429, {"retry-after": "120"}, "")],
                  "/api/v1/skills/y": [(200, {}, skill_body("o"))]}
        c, http, _ = client(routes, max_attempts=1, retry_after_cap=60)
        r = c.skill("x", None)
        self.assertEqual((r.status, r.reason), ("fetch_failed", "http_429"))
        self.assertEqual(c.halted, "retry_after_120s")
        self.assertEqual(c.sleeps, [])
        self.assertEqual(c.skill("y", None).status, "deferred")
        self.assertEqual(http.calls, ["/api/v1/skills/x"])

    def test_final_attempt_retry_after_cooldown_applies_to_next_skill(self):
        routes = {"/api/v1/skills/x": [(429, {"retry-after": "5"}, "")],
                  "/api/v1/skills/y": [(200, {}, skill_body("o"))]}
        c, http, _ = client(routes, max_attempts=1)
        self.assertEqual(c.skill("x", None).status, "fetch_failed")
        self.assertEqual(c.skill("y", None).status, "ok")
        self.assertEqual(http.calls, ["/api/v1/skills/x", "/api/v1/skills/y"])
        self.assertEqual(c.attempt_times[1] - c.attempt_times[0], 5.0)

    def test_retry_after_http_date_beyond_remaining_time_halts(self):
        wall = datetime(2026, 9, 27, 14, 40, 0, tzinfo=timezone.utc)
        c, http, clock = client({"/api/v1/skills/x": [(503, {"retry-after": "Sun, 27 Sep 2026 14:40:50 GMT"}, "")]},
                                deadline_s=100, retry_after_cap=60, wallclock=lambda: wall)
        clock.t = 70  # 30 s left in the run, server asks for 50 s
        self.assertEqual(c.skill("x", None).status, "fetch_failed")
        self.assertEqual(c.sleeps, [])
        self.assertTrue(c.halted)

    def test_timeout_then_success(self):
        c, _, _ = client({"/api/v1/skills/x": [(0, {}, ""), (200, {}, skill_body("Asif2BD", downloads=5))]})
        r = c.skill("x", "asif2bd")
        self.assertEqual((r.status, r.owner, r.stats["downloads"]), ("ok", "Asif2BD", 5))
        self.assertEqual(r.url, "https://clawhub.ai/Asif2BD/skills/x")

    def test_404_is_not_found_without_retry(self):
        c, http, _ = client({"/api/v1/skills/x": [(404, {}, "Skill not found")]})
        self.assertEqual(c.skill("x", None).status, "not_found")
        self.assertEqual(len(http.calls), 1)

    def test_401_and_403_are_fetch_failed(self):
        for status in (401, 403):
            c, http, _ = client({"/api/v1/skills/x": [(status, {}, "Unauthorized")]})
            r = c.skill("x", None)
            self.assertEqual((r.status, r.reason), ("fetch_failed", f"http_{status}"))
            self.assertEqual(len(http.calls), 1)

    def test_ambiguous_slug_uses_search_to_pick_owner(self):
        search = json.dumps({"results": [
            {"canonicalUrl": "/steipete/skills/gog", "native": {"ownerHandle": "steipete", "skill": {"slug": "gog", "stats": {"downloads": 9}}}},
            {"canonicalUrl": "/thcjp/skills/gog", "native": {"ownerHandle": "thcjp", "skill": {"slug": "gog", "stats": {"downloads": 222, "installs": 1, "stars": 0}}}},
        ]})
        routes = {"/api/v1/skills/gog": [(409, {}, '{"code":"AMBIGUOUS_SKILL_SLUG"}')], "/api/v1/search?q=gog": [(200, {}, search)]}
        c, _, _ = client(routes)
        r = c.skill("gog", "thcjp")
        self.assertEqual((r.status, r.owner, r.stats["downloads"], r.url), ("ok", "thcjp", 222, "https://clawhub.ai/thcjp/skills/gog"))
        c2, _, _ = client(routes)
        self.assertEqual(c2.skill("gog", None).reason, "ambiguous_slug_no_owner")
        c3, _, _ = client(routes)
        self.assertEqual((c3.skill("gog", "nobody").status, c3.skill("gog", "nobody").reason), ("not_found", "ambiguous_no_owner_match"))

    def test_owner_mismatch_is_not_found(self):
        c, _, _ = client({"/api/v1/skills/x": [(200, {}, skill_body("someone-else"))],
                          "/api/v1/search?q=x": [(200, {}, '{"results": []}')]})
        r = c.skill("x", "asif2bd")
        self.assertEqual((r.status, r.reason), ("not_found", "owner_mismatch"))

    def test_incomplete_payload_is_transient(self):
        c, _, _ = client({"/api/v1/skills/x": [(200, {}, '{"skill": {"stats": {}}, "owner": {"handle": "o"}}')]})
        self.assertEqual(c.skill("x", None).status, "fetch_failed")

    def test_cache_definitive_only(self):
        with tempfile.TemporaryDirectory() as d:
            cache = DiskCache(Path(d) / "c.json")
            c, http, _ = client({"/api/v1/skills/x": [(200, {}, skill_body("o"))], "/api/v1/skills/y": [(503, {}, "")]},
                                cache=cache, max_attempts=1)
            c.skill("x", None)
            c.skill("x", None)
            c.skill("y", None)
            c.skill("y", None)
            self.assertEqual(http.calls.count("/api/v1/skills/x"), 1)  # served from cache
            self.assertEqual(http.calls.count("/api/v1/skills/y"), 2)  # failures are not cached

    def test_retries_count_against_request_cap(self):
        c, http, _ = client({"/api/v1/skills/x": [(503, {}, "")], "/api/v1/skills/y": [(200, {}, skill_body("o"))]},
                            max_requests=2, max_attempts=3)
        r = c.skill("x", None)
        self.assertEqual((r.status, r.reason), ("fetch_failed", "http_503"))  # 2 real failed attempts, not 3
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(c.requests, 2)
        self.assertEqual(c.budget_left(), 0)
        self.assertEqual(c.skill("y", None).status, "deferred")  # cap reached by retries: no further request
        self.assertEqual(len(http.calls), 2)

    def test_cap_counts_every_attempt_across_skills(self):
        routes = {f"/api/v1/skills/s{i}": [(500, {}, "")] for i in range(10)}
        c, http, _ = client(routes, max_requests=7, max_attempts=3)
        for i in range(10):
            c.skill(f"s{i}", None)
        self.assertEqual(len(http.calls), 7)
        self.assertEqual(c.requests, 7)

    def test_pacing_applies_to_retries(self):
        # Retry-After: 0 and a zero backoff would allow immediate retries; the 1 s pacing must still hold.
        c, http, clock = client({"/api/v1/skills/x": [(429, {"retry-after": "0"}, ""), (503, {"retry-after": "0"}, ""),
                                                      (200, {}, skill_body("o"))],
                                 "/api/v1/skills/y": [(200, {}, skill_body("o"))]}, min_interval=1.0)
        self.assertEqual(c.skill("x", None).status, "ok")
        self.assertEqual(c.skill("y", None).status, "ok")
        gaps = [b - a for a, b in zip(c.attempt_times, c.attempt_times[1:])]
        self.assertEqual(len(c.attempt_times), 4)
        self.assertTrue(all(g >= 1.0 for g in gaps), gaps)

    def test_pacing_with_backoff_is_not_double_counted(self):
        c, _, _ = client({"/api/v1/skills/x": [(503, {}, ""), (200, {}, skill_body("o"))]}, min_interval=1.0)
        c.skill("x", None)
        self.assertEqual(c.sleeps, [1.5])  # backoff (1.5 s) already exceeds the 1 s pacing
        self.assertGreaterEqual(c.attempt_times[1] - c.attempt_times[0], 1.0)


class RefreshTests(unittest.TestCase):
    def test_ok_writes_counts_timestamps_and_canonical_url_only(self):
        c = cat()
        before = copy.deepcopy(c)
        cl, _, _ = client({"/api/v1/skills/ai-coding-token-optimizer": [(200, {}, skill_body("asif2bd", 120, 2))],
                           "/api/v1/skills/brex": [(404, {}, "")],
                           "/api/v1/skills/gog": [(503, {}, "")]}, max_attempts=2)
        s = S.refresh(c, cl, now=NOW)
        a, b, g, d = c["skills"]
        er = a["external_ratings"]
        self.assertEqual((er["clawhub_downloads"], er["clawhub_installs"]), (120, 2))
        self.assertEqual(er["clawhub_stats_status"], "ok")
        self.assertEqual(er["clawhub_last_success_at"], "2026-09-27T12:00:00Z")
        self.assertEqual(er["clawhub_fetched_at"], "2026-09-27T12:00:00Z")
        self.assertEqual(er["clawhub_url"], "https://clawhub.ai/asif2bd/skills/ai-coding-token-optimizer")
        # brex: not found; its legacy undated count (57) is dropped from the public catalog, no success time
        # invented, url untouched
        eb = b["external_ratings"]
        self.assertEqual((eb["clawhub_stats_status"], eb["clawhub_stats_reason"]), ("not_found", "http_404"))
        self.assertNotIn("clawhub_downloads", eb)
        self.assertEqual(s["sanitized"], {"rows": 1, "fields": 1})
        self.assertNotIn("clawhub_last_success_at", eb)
        self.assertEqual(eb["clawhub_url"], "https://clawhub.ai/skills/brex")
        # gog: transient
        eg = g["external_ratings"]
        self.assertEqual(eg["clawhub_stats_status"], "fetch_failed")
        self.assertNotIn("clawhub_downloads", eg)  # never writes zero
        self.assertEqual(d, before["skills"][3])
        for x, y in zip(c["skills"], before["skills"]):
            for k in ("id", "slug", "category", "repo_url"):
                self.assertEqual(x.get(k), y.get(k))
        self.assertEqual((len(s["ok"]), len(s["not_found"]), len(s["fetch_failed"])), (1, 1, 1))

    def test_failed_refresh_keeps_last_good_values_and_retrieval_time(self):
        for status in (0, 429, 500, 503, 404):
            c = cat()
            er = c["skills"][0]["external_ratings"]
            last_good(er)
            cl, _, _ = client({"/api/v1/skills/ai-coding-token-optimizer": [(status, {}, "")]}, max_attempts=2)
            S.refresh(c, cl, now=NOW, only=["ai-coding-token-optimizer"])
            self.assertEqual(er["clawhub_downloads"], 119, status)
            self.assertEqual(er["clawhub_installs"], 2, status)
            self.assertEqual(er["clawhub_last_success_at"], T0, status)
            self.assertEqual(er["clawhub_url"], "https://clawhub.ai/asif2bd/skills/ai-coding-token-optimizer")
            self.assertEqual(er["clawhub_fetched_at"], "2026-09-27T12:00:00Z")
            self.assertEqual(er["clawhub_stats_status"], "not_found" if status == 404 else "fetch_failed")

    def test_ttl_failed_retry_and_cap(self):
        c = cat()
        routes = {"/api/v1/skills/ai-coding-token-optimizer": [(200, {}, skill_body("asif2bd"))],
                  "/api/v1/skills/brex": [(404, {}, "")], "/api/v1/skills/gog": [(503, {}, "")]}
        cl, http, _ = client(routes, max_attempts=1)
        S.refresh(c, cl, now=NOW)
        cl, http, _ = client(routes, max_attempts=1)
        S.refresh(c, cl, now=NOW + timedelta(minutes=30))
        self.assertEqual(http.calls, [])  # everything fresh
        cl, http, _ = client(routes, max_attempts=1)
        S.refresh(c, cl, now=NOW + timedelta(hours=2))
        self.assertEqual(http.calls, ["/api/v1/skills/gog"])  # only fetch_failed retried after 1 h
        cl, http, _ = client(routes, max_attempts=1, max_requests=1)
        s = S.refresh(c, cl, now=NOW + timedelta(hours=25))
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(s["deferred"], 2)

    def test_deadline_defers_without_writing(self):
        c = cat()
        cl, http, clock = client({"/api/v1/skills/ai-coding-token-optimizer": [(0, {}, "")]}, deadline_s=10,
                                 latency=20.0, max_attempts=3)
        s = S.refresh(c, cl, now=NOW)
        # first request burns the time budget; the rest are deferred and untouched
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(s["deferred"], 2)
        self.assertNotIn("clawhub_stats_status", c["skills"][1]["external_ratings"])

    def test_legacy_draft_fields_migrate(self):
        c = cat()
        er = c["skills"][0]["external_ratings"]
        er.update(clawhub_stats_at=T0, clawhub_stats_status="ok", clawhub_downloads=119)
        eb = c["skills"][1]["external_ratings"]
        eb.update(clawhub_stats_at=T0, clawhub_stats_status="unresolved")
        cl, _, _ = client({}, max_requests=0)
        S.refresh(c, cl, now=NOW)
        self.assertNotIn("clawhub_stats_at", er)
        self.assertEqual(er["clawhub_last_success_at"], T0)
        self.assertNotIn("clawhub_stats_status", eb)



class PublicSanitizeTests(unittest.TestCase):
    def test_undated_counts_removed_dated_kept(self):
        c = {"skills": [
            {"id": "legacy", "external_ratings": {"clawhub_downloads": 3864, "clawhub_stars": 3}},
            {"id": "placeholders", "external_ratings": {"clawhub_downloads": 0, "clawhub_installs": 0, "clawhub_rating": None,
                                                       "github_stars": 5}},
            {"id": "nf-undated", "external_ratings": {"clawhub_downloads": 12, "clawhub_stats_status": "not_found",
                                                     "clawhub_fetched_at": T0}},
            {"id": "ff-undated", "external_ratings": {"clawhub_installs": 4, "clawhub_stats_status": "fetch_failed",
                                                     "clawhub_fetched_at": T0}},
            {"id": "nf-dated", "external_ratings": {"clawhub_downloads": 119, "clawhub_stats_status": "not_found",
                                                   "clawhub_last_success_at": T0}},
            {"id": "ff-dated", "external_ratings": {"clawhub_downloads": 119, "clawhub_stats_status": "fetch_failed",
                                                   "clawhub_last_success_at": T0}},
            {"id": "bad-date", "external_ratings": {"clawhub_downloads": 9, "clawhub_stats_status": "ok",
                                                   "clawhub_last_success_at": "%Y-%m-%dT%H:%M:%fZ"}},
            {"id": "no-er"},
        ]}
        self.assertEqual(S.sanitize_public(c), {"rows": 5, "fields": 8})
        er = {x["id"]: x.get("external_ratings", {}) for x in c["skills"]}
        for k in ("legacy", "placeholders", "nf-undated", "ff-undated", "bad-date"):
            self.assertFalse(any(f in er[k] for f in S.COUNT_FIELDS), k)
        self.assertEqual(er["placeholders"]["github_stars"], 5)  # only ClawHub counts are touched
        self.assertEqual(er["nf-dated"]["clawhub_downloads"], 119)
        self.assertEqual(er["ff-dated"]["clawhub_downloads"], 119)
        self.assertEqual(S.sanitize_public(c), {"rows": 0, "fields": 0})  # idempotent

    def test_refresh_sanitizes_even_with_zero_budget(self):
        c = cat()
        cl, http, _ = client({}, max_requests=0)
        s = S.refresh(c, cl, now=NOW)
        self.assertEqual(http.calls, [])
        self.assertNotIn("clawhub_downloads", c["skills"][1]["external_ratings"])
        self.assertEqual(s["sanitized"]["rows"], 1)

    def test_refresh_halts_on_long_retry_after_and_defers_rest(self):
        c = cat()
        cl, http, _ = client({"/api/v1/skills/ai-coding-token-optimizer": [(429, {"retry-after": "3600"}, "")]})
        last_good(c["skills"][0]["external_ratings"])
        c["skills"][0]["external_ratings"]["clawhub_fetched_at"] = "2026-01-01T00:00:00Z"  # stalest: first in queue
        for x in c["skills"][1:3]:
            x["external_ratings"]["clawhub_fetched_at"] = "2026-09-26T00:00:00Z"
        s = S.refresh(c, cl, now=NOW, only=["ai-coding-token-optimizer", "brex", "gog-listing"])
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(s["halted"], "retry_after_3600s")
        self.assertEqual(s["deferred"], 2)
        er = c["skills"][0]["external_ratings"]
        self.assertEqual((er["clawhub_stats_status"], er["clawhub_downloads"], er["clawhub_last_success_at"]),
                         ("fetch_failed", 119, T0))


if __name__ == "__main__":
    unittest.main()
