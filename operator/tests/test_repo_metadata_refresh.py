"""Repo metadata refresh: field rules, resume, unavailable repos, publisher window. No network."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import repo_metadata_refresh as R  # noqa: E402
import scout  # noqa: E402
from publish_lib import catalog_json_dumps  # noqa: E402
from publish_run import ADDED_RE, CATALOG_REL, DHAKA, HEAD_PREFIX, TITLE_RE  # noqa: E402
from skill_files import _canon  # noqa: E402

NOW = "2026-11-01T16:10:00Z"
PLUGIN = json.dumps({"name": "rederive", "version": "0.1.0", "description": "Learn a codebase"})
README = "<p>rederive</p>\n"
SKILL_MD = "---\nname: rederive\ndescription: Teaches a codebase\n---\n\n# Rederive\n"
OUTSIDE = datetime(2026, 11, 1, 10, 20, tzinfo=DHAKA)


def _scores() -> dict:
    return {
        "functionality": 8, "documentation": 8, "security": 8, "maintenance": 8,
        "usefulness": 8, "uniqueness": 8, "code_quality": 8, "average": 8,
    }


def rederive_row(**overrides) -> dict:
    row = {
        "id": "ericjujianzou-rederive-skills-rederive",
        "slug": "ericjujianzou-rederive-skills-rederive",
        "name": "rederive",
        "category": "other",
        "description": "Teaches a codebase",
        "author": "ericjujianzou",
        "author_url": "https://github.com/ericjujianzou",
        "version": "0.0.0",
        "repo_url": "https://github.com/ericjujianzou/rederive",
        "works_with": ["claude-code", "codex", "cursor"],
        "paid": False,
        "price": 0,
        "verified_at": "2026-11-01",
        "featured": False,
        "reviewed": True,
        "is_collection": False,
        "scores": _scores(),
        "github_stars": 14,
        "skill_path": "skills/rederive",
        "source_url": "https://github.com/ericjujianzou/rederive/tree/master/skills/rederive",
        "review_version": "catalog-review/1",
    }
    row.update(overrides)
    return row


def rederive_fetch(**overrides) -> dict:
    files = {
        "skills/rederive/.claude-plugin/plugin.json": None,
        ".claude-plugin/plugin.json": PLUGIN,
        "skills/rederive/plugin.json": None,
        "skills/rederive/package.json": None,
        "package.json": json.dumps({"version": "9.9.9"}),
        "skills/rederive/SKILL.md": SKILL_MD,
        "SKILL.md": "---\nname: other\nversion: 3.0.0\n---\n",
        "skills/rederive/README.md": None,
        "README.md": README,
    }
    files.update(overrides.pop("files", {}))
    fetched = {
        "stars": 378,
        "open_issues": 0,
        "homepage": "https://ericzou.dev/rederive/",
        "last_commit": "2026-10-25T20:26:46Z",
        "author_github": "https://github.com/EricJujianZou",
        "author_avatar": "https://avatars.githubusercontent.com/u/182662111?v=4",
        "renamed_to": None,
        "files": files,
    }
    fetched.update(overrides)
    return fetched


def _skill(slug: str, stars: int, repo: str) -> dict:
    return {
        "id": slug, "slug": slug, "category": "coding", "reviewed": False,
        "scores": _scores(), "version": "0.0.0", "github_stars": stars,
        "repo_url": f"https://github.com/{repo}", "author": "ex",
    }


class Proc:
    def __init__(self, stdout: str = "", code: int = 0):
        self.stdout, self.stderr, self.returncode = stdout, "", code


def run_main(argv: list[str], **kwargs) -> tuple[int, dict]:
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            R.main(argv, **kwargs)
        except SystemExit as exc:
            code = int(exc.code or 0)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else {}
    return code, payload


class TestFieldRules(unittest.TestCase):
    def test_rederive_reference_shape_and_locked_fields(self):
        row = rederive_row()
        outcome = R.refresh_skill_metadata(row, rederive_fetch(), now=NOW)
        self.assertEqual(outcome["status"], "updated")
        skill = outcome["skill"]
        self.assertEqual(skill["github_stars"], 378)
        self.assertEqual(skill["last_commit"], "2026-10-25T20:26:46Z")
        self.assertEqual(skill["open_issues"], 0)
        self.assertEqual(skill["homepage"], "https://ericzou.dev/rederive/")
        self.assertEqual(skill["version"], "0.1.0")
        self.assertEqual(skill["author_github"], "https://github.com/EricJujianZou")
        self.assertEqual(skill["author_avatar"], "https://avatars.githubusercontent.com/u/182662111?v=4")
        self.assertEqual(skill["readme"], README)
        self.assertEqual(skill["last_synced_at"], NOW)
        for key in ("id", "slug", "category", "reviewed", "works_with", "repo_url", "author"):
            self.assertEqual(skill[key], row[key])
        self.assertEqual(skill["scores"], row["scores"])
        self.assertEqual(outcome["changes"]["version"], {"from": "0.0.0", "to": "0.1.0"})
        self.assertEqual(outcome["changes"]["github_stars"], {"from": 14, "to": 378})
        self.assertEqual(outcome["changes"]["readme"]["to_bytes"], len(README))

    def test_second_apply_is_a_no_op(self):
        once = R.refresh_skill_metadata(rederive_row(), rederive_fetch(), now=NOW)["skill"]
        twice = R.refresh_skill_metadata(once, rederive_fetch(), now="2026-11-02T00:00:00Z")
        self.assertEqual(twice["status"], "unchanged")
        self.assertIs(twice["skill"], once)
        self.assertEqual(_canon(twice["skill"]), _canon(once))
        self.assertEqual(once["last_synced_at"], NOW)

    def test_version_priority_and_placeholders(self):
        self.assertIsNone(R.real_version("0.0.0"))
        self.assertIsNone(R.real_version("v0.0.0"))
        self.assertEqual(R.real_version("0.1.0"), "0.1.0")
        folder = "skills/rederive"
        files = rederive_fetch()["files"]
        self.assertEqual(R.pick_version(folder, files), "0.1.0")
        package_only = {"package.json": json.dumps({"version": "0.0.0"}), "SKILL.md": "---\nversion: 1.4.2\n---\n"}
        self.assertEqual(R.pick_version(None, package_only), "1.4.2")
        self.assertIsNone(R.pick_version(None, {"package.json": "{", "SKILL.md": "---\nname: x\n---\n"}))
        kept = R.refresh_skill_metadata(
            rederive_row(version="1.2.3"),
            rederive_fetch(files={".claude-plugin/plugin.json": json.dumps({"version": "0.0.0"}),
                                  "package.json": None, "SKILL.md": None, "skills/rederive/SKILL.md": "---\nname: rederive\n---\n"}),
            now=NOW,
        )
        self.assertEqual(kept["skill"]["version"], "1.2.3")

    def test_readme_updates_only_when_upstream_changes(self):
        row = rederive_row(readme=README)
        same = R.refresh_skill_metadata(row, rederive_fetch(), now=NOW)
        self.assertNotIn("readme", same["changes"])
        changed = R.refresh_skill_metadata(row, rederive_fetch(files={"README.md": README + "more\n"}), now=NOW)
        self.assertEqual(changed["skill"]["readme"], README + "more\n")
        long = "x" * 9000
        capped = R.refresh_skill_metadata(rederive_row(), rederive_fetch(files={"README.md": long}), now=NOW)
        self.assertEqual(len(capped["skill"]["readme"]), R.README_CAP)
        again = R.refresh_skill_metadata(capped["skill"], rederive_fetch(files={"README.md": long + "y"}), now=NOW)
        self.assertNotIn("readme", again["changes"])
        self.assertEqual(len(again["skill"]["readme"]), R.README_CAP)
        missing = R.refresh_skill_metadata(
            rederive_row(readme="keep me"),
            rederive_fetch(files={"README.md": None, "skills/rederive/README.md": None}),
            now=NOW,
        )
        self.assertEqual(missing["skill"]["readme"], "keep me")

    def test_author_filled_only_when_missing_and_homepage_not_cleared(self):
        row = rederive_row(
            author_github="https://github.com/anthropics",
            author_avatar="https://avatars.githubusercontent.com/u/1?v=4",
            homepage="https://keep.example",
        )
        outcome = R.refresh_skill_metadata(row, rederive_fetch(homepage=None), now=NOW)
        self.assertEqual(outcome["skill"]["author_github"], "https://github.com/anthropics")
        self.assertEqual(outcome["skill"]["author_avatar"], "https://avatars.githubusercontent.com/u/1?v=4")
        self.assertEqual(outcome["skill"]["homepage"], "https://keep.example")
        blank = R.refresh_skill_metadata(rederive_row(author_github="  "), rederive_fetch(), now=NOW)
        self.assertEqual(blank["skill"]["author_github"], "https://github.com/EricJujianZou")

    def test_avatar_and_commit_time_normalization(self):
        self.assertEqual(
            R.canonical_avatar("https://avatars.githubusercontent.com/u/182662111?s=400&v=4"),
            "https://avatars.githubusercontent.com/u/182662111?v=4",
        )
        self.assertEqual(
            R.canonical_avatar("https://avatars.githubusercontent.com/u/5697919?u=aea7380c2f6bfb3cebdc34f972cbdc565de2178e&v=4"),
            "https://avatars.githubusercontent.com/u/5697919?v=4",
        )
        self.assertEqual(R.normalize_commit_time("2026-10-25T22:26:46+02:00"), "2026-10-25T20:26:46Z")

    def test_publisher_window_bounds(self):
        def at(minute: int) -> datetime:
            return datetime(2026, 11, 1, 10, minute, tzinfo=DHAKA)
        self.assertFalse(R.in_publisher_window(at(43)))
        self.assertTrue(R.in_publisher_window(at(44)))
        self.assertTrue(R.in_publisher_window(at(58)))
        self.assertFalse(R.in_publisher_window(at(59)))


class TestFetch(unittest.TestCase):
    def test_one_graphql_query_covers_a_batch_and_a_missing_repo(self):
        specs = [
            {"key": "ericjujianzou/rederive", "owner": "ericjujianzou", "repo": "rederive",
             "paths": [".claude-plugin/plugin.json", "README.md"]},
            {"key": "gone/repo", "owner": "gone", "repo": "repo", "paths": ["README.md"]},
        ]
        payload = {
            "data": {
                "r0": {
                    "nameWithOwner": "EricJujianZou/rederive",
                    "stargazerCount": 378,
                    "homepageUrl": "https://ericzou.dev/rederive/",
                    "issues": {"totalCount": 0},
                    "defaultBranchRef": {"name": "master", "target": {"committedDate": "2026-10-25T20:26:46Z"}},
                    "owner": {"login": "EricJujianZou", "avatarUrl": "https://avatars.githubusercontent.com/u/182662111?s=80&v=4"},
                    "f0": {"text": PLUGIN, "byteSize": len(PLUGIN)},
                    "f1": {"text": README, "byteSize": len(README)},
                },
                "r1": None,
            },
            "errors": [{"type": "NOT_FOUND", "path": ["r1"]}],
        }

        class Client:
            def __init__(self):
                self.queries: list[str] = []

            def graphql(self, query: str) -> dict:
                self.queries.append(query)
                self.assert_query(query)
                return payload

            def assert_query(self, query: str) -> None:
                self_test = self
                assert query.count("repository(") == 2
                assert "issues(states: [OPEN])" in query
                assert "stargazerCount" in query
                assert "HEAD:.claude-plugin/plugin.json" in query

        client = Client()
        out = R.fetch_refresh(client, specs, batch=20)
        self.assertEqual(len(client.queries), 1)
        got = out["ericjujianzou/rederive"]
        self.assertEqual(got["stars"], 378)
        self.assertEqual(got["open_issues"], 0)
        self.assertEqual(got["last_commit"], "2026-10-25T20:26:46Z")
        self.assertEqual(got["author_avatar"], "https://avatars.githubusercontent.com/u/182662111?v=4")
        self.assertIsNone(got["renamed_to"])
        self.assertEqual(got["files"][".claude-plugin/plugin.json"], PLUGIN)
        self.assertEqual(out["gone/repo"], "not_found")

    def test_failed_batch_is_retried_per_repository(self):
        specs = [
            {"key": "a/one", "owner": "a", "repo": "one", "paths": []},
            {"key": "b/two", "owner": "b", "repo": "two", "paths": []},
        ]
        calls: list[int] = []

        class Client:
            def graphql(self, query: str) -> dict:
                calls.append(query.count("repository("))
                if query.count("repository(") > 1:
                    raise scout.Transient("HTTP 502")
                owner = "a" if "one" in query else "b"
                repo = "one" if owner == "a" else "two"
                return {"data": {"r0": {
                    "nameWithOwner": f"{owner}/{repo}", "stargazerCount": 3, "homepageUrl": None,
                    "issues": {"totalCount": 1},
                    "defaultBranchRef": {"target": {"committedDate": "2026-01-01T00:00:00Z"}},
                    "owner": {"login": owner, "avatarUrl": None},
                }}, "errors": []}

        out = R.fetch_refresh(Client(), specs, batch=10)
        self.assertEqual(calls, [2, 1, 1])
        self.assertEqual(out["a/one"]["stars"], 3)
        self.assertEqual(out["b/two"]["open_issues"], 1)

    def test_invalid_repo_name_is_not_found_without_a_query(self):
        class Client:
            def graphql(self, query: str) -> dict:
                raise AssertionError("should not query")

        out = R.fetch_refresh(Client(), [{"key": "bad name/repo", "owner": "bad name", "repo": "repo", "paths": []}])
        self.assertEqual(out["bad name/repo"], "not_found")


class TestRun(unittest.TestCase):
    def catalog(self) -> dict:
        return {
            "version": "1.1.0",
            "total": 4,
            "skills": [
                _skill("zeta", 50, "ex/zeta"),
                rederive_row(),
                _skill("alpha", 2, "ex/alpha"),
                {"id": "claw", "slug": "claw", "category": "other", "reviewed": False, "scores": _scores(),
                 "repo_url": "https://clawhub.ai/a/skills/claw", "source_type": "clawhub", "version": "0.0.0"},
            ],
        }

    def fetch(self, specs: list[dict]) -> dict:
        out = {}
        for spec in specs:
            if spec["key"] == "ericjujianzou/rederive":
                out[spec["key"]] = rederive_fetch()
            elif spec["key"] == "ex/zeta":
                out[spec["key"]] = {"stars": 50, "open_issues": 1, "homepage": None, "last_commit": "2026-01-01T00:00:00Z",
                                    "author_github": "https://github.com/ex", "author_avatar": None, "files": {}}
            else:
                out[spec["key"]] = {"stars": 4, "open_issues": 0, "homepage": None, "last_commit": "2026-01-02T00:00:00Z",
                                    "author_github": None, "author_avatar": None, "files": {}}
        return out

    def test_traffic_order_limit_and_resume(self):
        ranking = {"ericjujianzou-rederive-skills-rederive": 5000, "alpha": 10}
        with tempfile.TemporaryDirectory() as tmp:
            state = R.RefreshState(Path(tmp) / "state.json")
            first = R.run_refresh(
                self.catalog(), ranking, state=state, apply=True, limit=1, now=NOW, fetch=self.fetch,
            )
            self.assertEqual(first["planned"], ["ericjujianzou-rederive-skills-rederive"])
            self.assertEqual([row["slug"] for row in first["order"]], [
                "ericjujianzou-rederive-skills-rederive", "alpha", "zeta",
            ])
            self.assertNotIn("claw", [row["slug"] for row in first["order"]])
            for item in first["inspected"]:
                if item["status"] != "transient":
                    state.complete(item["slug"], item["status"], NOW)
            state.save()
            state.load()
            second = R.run_refresh(
                first["catalog"], ranking, state=state, apply=True, limit=1, now=NOW, fetch=self.fetch,
            )
            self.assertEqual(second["planned"], ["alpha"])
            self.assertEqual(len(second["catalog"]["skills"]), 4)
            self.assertEqual(second["catalog"]["total"], 4)

    def test_progress_survives_catalog_growth(self):
        ranking = {"ericjujianzou-rederive-skills-rederive": 5000, "alpha": 10}
        with tempfile.TemporaryDirectory() as tmp:
            state = R.RefreshState(Path(tmp) / "state.json")
            first = R.run_refresh(
                self.catalog(), ranking, state=state, apply=True, limit=1, now=NOW, fetch=self.fetch,
            )
            self.assertEqual(first["planned"], ["ericjujianzou-rederive-skills-rederive"])
            for item in first["inspected"]:
                if item["status"] != "transient":
                    state.complete(item["slug"], item["status"], NOW)
            state.save()
            grown = self.catalog()
            grown["skills"] = list(grown["skills"]) + [_skill("newbie", 0, "ex/newbie")]
            grown["total"] = len(grown["skills"])
            state.load()
            second = R.run_refresh(
                grown, ranking, state=state, apply=True, limit=1, now=NOW, fetch=self.fetch,
            )
            self.assertEqual(second["planned"], ["alpha"])
            self.assertIn("newbie", [row["slug"] for row in second["order"]])
            self.assertIn("ericjujianzou-rederive-skills-rederive", state.data["completed"])
            self.assertNotIn("newbie", state.data["completed"])
            self.assertEqual(state.data["version"], 1)

    def test_not_found_is_recorded_and_the_row_stays(self):
        catalog = {"total": 1, "skills": [rederive_row()]}

        def fetch(specs):
            return {spec["key"]: "not_found" for spec in specs}

        with tempfile.TemporaryDirectory() as tmp:
            state = R.RefreshState(Path(tmp) / "state.json")
            result = R.run_refresh(catalog, {}, state=state, apply=True, limit=5, now=NOW, fetch=fetch)
            self.assertEqual(result["updated"], [])
            self.assertEqual(result["unavailable"][0]["reason"], "deleted_private_or_missing")
            self.assertEqual(result["catalog"]["skills"][0]["id"], catalog["skills"][0]["id"])
            self.assertEqual(result["catalog"]["skills"][0]["github_stars"], 14)
            self.assertEqual(state.data["unavailable"]["ericjujianzou/rederive"]["reason"], "deleted_private_or_missing")
            self.assertEqual(len(state.data["unavailable_log"]), 1)
            self.assertEqual(result["diff_bytes"], 0)

    def test_rename_is_recorded_without_changing_repo_url(self):
        catalog = {"skills": [rederive_row()]}

        def fetch(specs):
            got = rederive_fetch(renamed_to="NewOrg/rederive")
            return {spec["key"]: got for spec in specs}

        with tempfile.TemporaryDirectory() as tmp:
            state = R.RefreshState(Path(tmp) / "state.json")
            result = R.run_refresh(catalog, {}, state=state, apply=True, limit=5, now=NOW, fetch=fetch)
            skill = result["catalog"]["skills"][0]
            self.assertEqual(skill["repo_url"], "https://github.com/ericjujianzou/rederive")
            self.assertEqual(skill["github_stars"], 378)
            self.assertEqual(skill["id"], "ericjujianzou-rederive-skills-rederive")
            self.assertEqual(result["renames"], [{"from": "ericjujianzou/rederive", "to": "NewOrg/rederive"}])
            self.assertEqual(state.data["renames"]["ericjujianzou/rederive"], "NewOrg/rederive")

    def test_transient_retries_once_then_moves_on_without_editing_the_row(self):
        catalog = {"skills": [_skill("alpha", 2, "ex/alpha"), _skill("zeta", 5, "ex/zeta")]}

        def fetch(specs):
            return {spec["key"]: "transient" for spec in specs}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = R.RefreshState(path)
            first = R.run_refresh(catalog, {}, state=state, apply=True, limit=5, now=NOW, fetch=fetch)
            self.assertEqual([item["status"] for item in first["inspected"]], ["transient", "transient"])
            self.assertEqual(_canon(first["catalog"]), _canon(catalog))
            for item in first["inspected"]:
                if item["status"] != "transient":
                    state.complete(item["slug"], item["status"], NOW)
            state.save()
            state.load()
            second = R.run_refresh(catalog, {}, state=state, apply=True, limit=5, now=NOW, fetch=fetch)
            self.assertEqual([item["status"] for item in second["inspected"]], ["error", "error"])
            self.assertEqual(second["catalog"]["skills"][0]["github_stars"], 2)
            for item in second["inspected"]:
                state.complete(item["slug"], item["status"], NOW)
            state.finish_if_done([row["slug"] for row in second["order"]])
            self.assertTrue(state.data["finished"])
            third = R.run_refresh(catalog, {}, state=state, apply=True, limit=5, now=NOW, fetch=fetch)
            self.assertEqual([item["status"] for item in third["inspected"]], ["transient", "transient"])


class TestCli(unittest.TestCase):
    def test_dry_run_prints_a_summary_and_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            catalog = {"skills": [rederive_row(), _skill("zeta", 1, "ex/zeta")], "total": 2}
            cat = tmp_path / "skills-catalog.json"
            cat.write_text(catalog_json_dumps(catalog) + "\n", encoding="utf-8")
            rank = tmp_path / "rank.csv"
            rank.write_text("slug,views\nericjujianzou-rederive-skills-rederive,9\n", encoding="utf-8")
            state = tmp_path / "state.json"
            code, payload = run_main(
                ["--catalog", str(cat), "--ranking", str(rank), "--state", str(state), "--limit", "5"],
                fetch=lambda specs: {spec["key"]: rederive_fetch() if spec["repo"] == "rederive" else {
                    "stars": 1, "open_issues": 0, "homepage": None, "last_commit": None,
                    "author_github": None, "author_avatar": None, "files": {},
                } for spec in specs},
                now=OUTSIDE,
            )
            self.assertEqual(code, 0, payload)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["updated"][0], "ericjujianzou-rederive-skills-rederive")
            self.assertGreater(payload["diff_bytes"], 0)
            self.assertIn("github_stars", payload["changes"]["ericjujianzou-rederive-skills-rederive"])
            self.assertIn("from_bytes", json.dumps(payload["changes"]))
            self.assertNotIn(README, json.dumps(payload))
            self.assertFalse(state.exists())
            self.assertEqual(cat.read_text(encoding="utf-8"), catalog_json_dumps(catalog) + "\n")

    def test_apply_window_and_open_pr_do_not_fetch_or_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cat = tmp_path / "skills-catalog.json"
            cat.write_text(catalog_json_dumps({"skills": [rederive_row()]}) + "\n", encoding="utf-8")
            website = tmp_path / "website"
            website.mkdir()
            state = tmp_path / "state.json"

            def boom(specs):
                raise AssertionError("should not fetch")

            code, payload = run_main(
                ["--catalog", str(cat), "--state", str(state), "--apply", "--website", str(website), "--limit", "5"],
                fetch=boom, now=datetime(2026, 11, 1, 10, 44, tzinfo=DHAKA),
            )
            self.assertEqual(code, 0)
            self.assertIn("WINDOW_SKIP", payload["message"])
            self.assertFalse(state.exists())

            def run(cmd, cwd):
                if list(cmd[:3]) == ["gh", "pr", "list"]:
                    return Proc(json.dumps([{"number": 9, "headRefName": "operator/catalog-refresh-9"}]))
                raise AssertionError(cmd)

            code, payload = run_main(
                ["--catalog", str(cat), "--state", str(state), "--apply", "--website", str(website), "--limit", "5"],
                fetch=boom, run=run, now=OUTSIDE,
            )
            self.assertEqual(code, 0)
            self.assertIn("open publish PR", payload["message"])
            self.assertEqual(payload["open_prs"], ["operator/catalog-refresh-9"])

    def test_apply_opens_a_catalog_only_pr_and_does_not_merge(self):
        commands: list[list[str]] = []

        def run(cmd, cwd):
            commands.append(list(cmd))
            if list(cmd[:3]) == ["git", "status", "--porcelain"]:
                return Proc("")
            if list(cmd[:4]) == ["git", "diff", "--cached", "--name-only"]:
                return Proc(CATALOG_REL + "\n")
            if list(cmd[:3]) == ["gh", "pr", "list"]:
                return Proc("[]")
            if list(cmd[:3]) == ["gh", "pr", "create"]:
                self.assertNotIn("--merge", cmd)
                body = cmd[cmd.index("--body") + 1]
                self.assertEqual(int(ADDED_RE.search(body).group(1)), 0)
                self.assertIsNone(TITLE_RE.search(cmd[cmd.index("--title") + 1]))
                self.assertIn("<!-- proskills:publish v1 items=[] -->", body)
                self.assertTrue(R.no_handles(body))
                return Proc("https://github.com/ProSkillsMD/website/pull/400\n")
            return Proc("")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cat = tmp_path / "skills-catalog.json"
            original = {"skills": [rederive_row()], "total": 1}
            cat.write_text(catalog_json_dumps(original) + "\n", encoding="utf-8")
            website = tmp_path / "website"
            (website / "public").mkdir(parents=True)
            state = tmp_path / "state.json"
            code, payload = run_main(
                ["--catalog", str(cat), "--state", str(state), "--apply", "--website", str(website),
                 "--limit", "5", "--stamp", "20261101-102000"],
                fetch=lambda specs: {spec["key"]: rederive_fetch() for spec in specs},
                run=run, now=OUTSIDE,
            )
            self.assertEqual(code, 0, payload)
            self.assertFalse(payload["dry_run"])
            self.assertTrue(payload["pr"]["ok"])
            self.assertFalse(payload["pr"]["merge_invoked"])
            self.assertFalse(any(cmd[:3] == ["gh", "pr", "merge"] for cmd in commands))
            branch = f"{HEAD_PREFIX}20261101-102000"
            self.assertTrue(any(branch in part for cmd in commands for part in cmd))
            written = json.loads((website / CATALOG_REL).read_text(encoding="utf-8"))
            self.assertEqual(written["skills"][0]["github_stars"], 378)
            self.assertEqual(written["skills"][0]["version"], "0.1.0")
            self.assertEqual(written["skills"][0]["id"], original["skills"][0]["id"])
            self.assertEqual(written["skills"][0]["scores"], original["skills"][0]["scores"])
            self.assertEqual(written["total"], 1)
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertIn("ericjujianzou-rederive-skills-rederive", saved["completed"])
            # A finished pass run again writes nothing when upstream is unchanged.
            commands.clear()
            code, again = run_main(
                ["--catalog", str(website / CATALOG_REL), "--state", str(state), "--apply",
                 "--website", str(website), "--limit", "5", "--stamp", "20261101-110000"],
                fetch=lambda specs: {spec["key"]: rederive_fetch() for spec in specs},
                run=run, now=datetime(2026, 11, 1, 10, 30, tzinfo=DHAKA),
            )
            self.assertEqual(code, 0, again)
            self.assertEqual(again["updated"], [])
            self.assertEqual(again["diff_bytes"], 0)
            self.assertFalse(any(cmd[:3] == ["gh", "pr", "create"] for cmd in commands))

    def test_cli_without_github_rows_does_not_need_a_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cat = tmp_path / "skills-catalog.json"
            cat.write_text(json.dumps({"skills": [{
                "id": "claw", "slug": "claw", "source_type": "clawhub",
                "repo_url": "https://clawhub.ai/a/skills/claw",
            }]}) + "\n", encoding="utf-8")
            state = tmp_path / "state.json"
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "repo_metadata_refresh.py"),
                 "--catalog", str(cat), "--state", str(state), "--limit", "5"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["updated"], [])
            self.assertEqual(payload["order_total"], 0)
            self.assertFalse(state.exists())

    def test_empty_catalog_diff_is_a_noop(self):
        commands: list[list[str]] = []

        def run(cmd, cwd):
            commands.append(list(cmd))
            if list(cmd[:3]) == ["git", "status", "--porcelain"]:
                return Proc("")
            if list(cmd[:4]) == ["git", "diff", "--cached", "--name-only"]:
                return Proc("")
            if list(cmd[:2]) in (["git", "commit"], ["git", "push"]) or list(cmd[:3]) == ["gh", "pr", "create"]:
                raise AssertionError(cmd)
            return Proc("")

        with tempfile.TemporaryDirectory() as tmp:
            out = R.push_catalog_pr(
                run, Path(tmp), {"skills": [rederive_row()], "total": 1},
                branch=f"{HEAD_PREFIX}20261101-120000", title="t", body="b",
            )
        self.assertTrue(out["ok"])
        self.assertFalse(out["opened"])
        self.assertNotEqual(out.get("reason"), "not_catalog_only")
        self.assertFalse(any(cmd[:2] == ["git", "commit"] for cmd in commands))


if __name__ == "__main__":
    unittest.main()
