"""Skill-folder capture, caps, refresh idempotency, and backfill order."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import scout  # noqa: E402
import skill_files as SF  # noqa: E402
from catalog_update import plan_update  # noqa: E402
from publish_lib import catalog_json_dumps  # noqa: E402
from publish_run import ADDED_RE, CATALOG_REL, DAILY_CAP, HEAD_PREFIX, TITLE_RE  # noqa: E402
import backfill_skill_files as BF  # noqa: E402

SKILL = "---\nname: rederive\ndescription: Rebuild a derivation\nlicense: MIT\n---\n\n# Rederive\n\nStep 1 runs scripts/history.sh.\n"
HISTORY = "#!/bin/sh\necho history\n"
STRUCTURE = "def structure():\n    return 1\n"


def _sha(text: str) -> str:
    return SF.git_blob_sha(text.encode("utf-8"))


def _entry(path: str, text: str | None = None, *, size: int | None = None, sha: str | None = None,
           symlink: bool = False) -> dict:
    raw = text.encode("utf-8") if text is not None else b""
    return {
        "path": path,
        "size": len(raw) if size is None else size,
        "sha": sha or (_sha(text) if text is not None else "ab" * 20),
        "symlink": symlink,
    }


class FakeGitHub:
    def __init__(self, trees: dict, blobs: dict | None = None):
        self.trees = trees
        self.blobs = blobs or {}
        self.calls: list[str] = []
        self.core_remaining = None

    def rest(self, path: str):
        self.calls.append(path)
        for key, value in self.trees.items():
            if key in path:
                return value
        raise scout.NotFound(path)

    def request(self, url: str, **_kwargs):
        self.calls.append(url)
        if ".." in url or "/.." in url or "%2E%2E" in url:
            raise AssertionError(f"fetch escaped the skill folder: {url}")
        for path, body in self.blobs.items():
            if quote(path) in url or path in url:
                return type("Resp", (), {"body": body, "status": 200})()
        raise scout.NotFound(url)


def _rederive_client() -> FakeGitHub:
    tree = {
        "sha": "folder",
        "truncated": False,
        "tree": [
            {"path": "SKILL.md", "type": "blob", "size": len(SKILL.encode()), "sha": _sha(SKILL), "mode": "100644"},
            {"path": "scripts/history.sh", "type": "blob", "size": len(HISTORY.encode()), "sha": _sha(HISTORY), "mode": "100644"},
            {"path": "scripts/structure.py", "type": "blob", "size": len(STRUCTURE.encode()), "sha": _sha(STRUCTURE), "mode": "100644"},
            {"path": "assets/logo.png", "type": "blob", "size": 40, "sha": "cd" * 20, "mode": "100644"},
            {"path": "../secret.txt", "type": "blob", "size": 4, "sha": "ef" * 20, "mode": "100644"},
            {"path": "scripts/../../outside.txt", "type": "blob", "size": 4, "sha": "11" * 20, "mode": "100644"},
            {"path": "node_modules/leftpad/index.js", "type": "blob", "size": 8, "sha": "22" * 20, "mode": "100644"},
        ],
    }
    blobs = {
        "skills/rederive/SKILL.md": SKILL.encode(),
        "skills/rederive/scripts/history.sh": HISTORY.encode(),
        "skills/rederive/scripts/structure.py": STRUCTURE.encode(),
    }
    return FakeGitHub({"skills/rederive": tree}, blobs)


class TestCapture(unittest.TestCase):
    def test_rederive_scripts_are_inlined_and_outside_paths_are_not_fetched(self):
        client = _rederive_client()
        captured = SF.capture_skill_folder(client, "ericjujianzou", "rederive", "skills/rederive", "main")
        self.assertTrue(captured["ok"], captured)
        self.assertFalse(captured["truncated"])
        by_path = {f["path"]: f for f in captured["files"]}
        self.assertEqual(set(by_path), {"SKILL.md", "scripts/history.sh", "scripts/structure.py", "assets/logo.png"})
        self.assertEqual(by_path["scripts/history.sh"]["content"], HISTORY)
        self.assertEqual(by_path["scripts/structure.py"]["content"], STRUCTURE)
        self.assertNotIn("content", by_path["assets/logo.png"])
        self.assertTrue(by_path["assets/logo.png"]["omitted"])
        self.assertIn("Step 1 runs scripts/history.sh.", by_path["SKILL.md"]["content"])
        joined = "\n".join(client.calls)
        self.assertIn("skills/rederive", joined)
        self.assertNotIn("secret", joined)
        self.assertNotIn("outside", joined)
        self.assertNotIn("leftpad", joined)
        for url in client.calls:
            if "raw.githubusercontent" in url:
                self.assertIn(quote("skills/rederive/"), url)

    def test_fetch_refuses_parent_path(self):
        client = FakeGitHub({}, {})
        with self.assertRaises(ValueError):
            SF.fetch_blob_bytes(client, "o", "r", "main", "skills/rederive", "../secret.txt")
        self.assertEqual(client.calls, [])

    def test_root_walk_skips_excluded_dirs_and_honors_dir_cap(self):
        child = {"tree": [{"path": "run.sh", "type": "blob", "size": 3, "sha": _sha("ok\n"), "mode": "100644"}], "truncated": False}
        trees = {
            "git/trees/main": {
                "sha": "root",
                "truncated": False,
                "tree": [
                    {"path": "SKILL.md", "type": "blob", "size": len(b"hi\n"), "sha": _sha("hi\n"), "mode": "100644"},
                    {"path": "node_modules", "type": "tree", "sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"},
                    {"path": "scripts", "type": "tree", "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                    {"path": "extra", "type": "tree", "sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
                ],
            },
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": child,
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": child,
        }
        client = FakeGitHub(trees, {})
        listed = SF.list_skill_tree(
            client, "o", "r", None, "main",
            limits={"max_dirs": 2, "max_calls": 400, "budget_s": 120},
            clock=lambda: 0,
        )
        self.assertTrue(listed["truncated"])
        self.assertEqual(listed["truncated_reason"], "walk_dir_cap")
        paths = [f["path"] for f in listed["files"]]
        self.assertIn("SKILL.md", paths)
        self.assertNotIn("node_modules/leftpad/index.js", paths)
        self.assertFalse(any("deadbeef" in c for c in client.calls))

    def test_time_budget_stops_the_walk(self):
        ticks = {"n": 0}

        def clock():
            ticks["n"] += 1
            return 0 if ticks["n"] == 1 else 1000

        trees = {
            "git/trees/main": {
                "sha": "root",
                "truncated": False,
                "tree": [
                    {"path": "scripts", "type": "tree", "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                ],
            },
        }
        client = FakeGitHub(trees, {})
        listed = SF.list_skill_tree(
            client, "o", "r", None, "main",
            limits={"max_dirs": 200, "max_calls": 400, "budget_s": 10},
            clock=clock,
        )
        self.assertTrue(listed["truncated"])
        self.assertEqual(listed["truncated_reason"], "time_budget")
        self.assertEqual(len(client.calls), 1)


class TestCaps(unittest.TestCase):
    def test_exact_cap_is_inlined_and_one_extra_byte_is_truncated(self):
        exact = "a" * SF.INLINE_MAX_BYTES
        over = "b" * (SF.INLINE_MAX_BYTES + 1)
        entries = [_entry("fit.txt", exact), _entry("over.txt", over)]
        fetched: list[str] = []

        def fetch(rel: str):
            fetched.append(rel)
            return {"fit.txt": exact, "over.txt": over}[rel].encode()

        files, _legacy = SF.assemble_files(entries, fetch, total_max=SF.SKILL_INLINE_MAX_BYTES * 4)
        by_path = {f["path"]: f for f in files}
        self.assertEqual(by_path["fit.txt"]["content"], exact)
        self.assertNotIn("truncated", by_path["fit.txt"])
        self.assertNotIn("content", by_path["over.txt"])
        self.assertTrue(by_path["over.txt"]["truncated"])
        self.assertTrue(by_path["over.txt"]["omitted"])
        self.assertNotIn("over.txt", fetched)

    def test_per_skill_budget_stops_inlining_after_priority_files(self):
        body = "x" * 100
        entries = [
            _entry("scripts/c.py", body),
            _entry("notes.txt", body),
            _entry("scripts/a.py", body),
            _entry("SKILL.md", body),
        ]
        files, _legacy = SF.assemble_files(entries, lambda rel: body.encode(), inline_max=1000, total_max=250)
        by_path = {f["path"]: f for f in files}
        self.assertIn("content", by_path["SKILL.md"])
        self.assertIn("content", by_path["scripts/a.py"])
        self.assertNotIn("content", by_path["scripts/c.py"])
        self.assertTrue(by_path["scripts/c.py"]["omitted"])
        self.assertNotIn("content", by_path["notes.txt"])
        self.assertTrue(by_path["notes.txt"]["omitted"])
        self.assertFalse(by_path["notes.txt"].get("truncated", False))

    def test_utf8_text_is_not_treated_as_binary(self):
        text = "说明\n"
        files, _legacy = SF.assemble_files([_entry("SKILL.md", text)], lambda rel: text.encode())
        self.assertEqual(files[0]["content"], text)


class TestRefresh(unittest.TestCase):
    def _skill(self) -> dict:
        return {
            "id": "rederive",
            "slug": "rederive",
            "name": "rederive",
            "description": "old",
            "author": "ericjujianzou",
            "author_url": "https://github.com/ericjujianzou",
            "license": "Apache-2.0",
            "attribution": "Eric Zou",
            "repo_url": "https://github.com/ericjujianzou/rederive",
            "skill_path": "skills/rederive",
            "category": "developer-tools",
            "skill_md": "old body",
            "files_found": ["skills/rederive/SKILL.md"],
            "github_stars": 3,
            "scores": {"average": 0},
        }

    def _captured(self, skill_md: str = SKILL, extra: dict | None = None) -> dict:
        files = [
            {"path": "SKILL.md", "size": len(skill_md.encode()), "sha": _sha(skill_md), "content": skill_md},
            {"path": "scripts/history.sh", "size": len(HISTORY.encode()), "sha": _sha(HISTORY), "content": HISTORY},
            {"path": "scripts/structure.py", "size": len(STRUCTURE.encode()), "sha": _sha(STRUCTURE), "content": STRUCTURE},
        ]
        captured = {
            "ok": True, "truncated": False, "missing_skill_md": False,
            "skill_md": skill_md, "readme": None, "files": files,
        }
        if extra:
            captured.update(extra)
        return captured

    def test_idempotent_and_preserves_attribution(self):
        skill = self._skill()
        captured = self._captured()
        first = SF.refresh_skill(skill, captured)
        self.assertEqual(first["status"], "updated")
        updated = first["skill"]
        self.assertEqual(updated["author"], "ericjujianzou")
        self.assertEqual(updated["license"], "Apache-2.0")
        self.assertEqual(updated["attribution"], "Eric Zou")
        self.assertEqual(updated["id"], "rederive")
        self.assertEqual(updated["repo_url"], skill["repo_url"])
        self.assertEqual(updated["category"], "developer-tools")
        self.assertIn("Rebuild a derivation", updated["skill_md"])
        self.assertIn("scripts/history.sh", [f["path"] for f in updated["files"]])
        self.assertIn("skills/rederive/SKILL.md", updated["files_found"])
        self.assertIn("scripts/history.sh", updated["files_found"])
        second = SF.refresh_skill(updated, captured)
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(SF._canon(second["skill"]), SF._canon(updated))

    def test_skill_md_refresh_tracks_upstream(self):
        skill = self._skill()
        newer = SKILL + "\nEdited upstream.\n"
        refreshed = SF.refresh_skill(skill, self._captured(newer))
        self.assertIn("Edited upstream.", refreshed["skill"]["skill_md"])
        again = SF.refresh_skill(refreshed["skill"], self._captured(newer))
        self.assertEqual(again["status"], "unchanged")

    def test_content_policy_flags_instead_of_updating(self):
        skill = self._skill()
        nsfw = "---\nname: pose\ndescription: Generate nude images\n---\n\nnsfw\n"
        outcome = SF.refresh_skill(skill, self._captured(nsfw))
        self.assertEqual(outcome["status"], "flag_review")
        self.assertEqual(outcome["reason"], "content_policy")
        self.assertTrue(outcome["hits"])
        self.assertEqual(outcome["skill"]["skill_md"], "old body")
        self.assertNotIn("files", outcome["skill"])

    def test_benign_moderation_text_is_not_flagged(self):
        skill = self._skill()
        clean = "---\nname: guard\ndescription: NSFW detection guardrail\n---\n\nfilter nsfw content\n"
        outcome = SF.refresh_skill(skill, self._captured(clean))
        self.assertEqual(outcome["status"], "updated")

    def test_truncated_listing_does_not_replace_files(self):
        skill = self._skill()
        captured = self._captured(extra={"truncated": True, "truncated_reason": "walk_dir_cap"})
        outcome = SF.refresh_skill(skill, captured)
        self.assertEqual(outcome["status"], "truncated")
        self.assertEqual(outcome["skill"], skill)


class TestBackfillOrder(unittest.TestCase):
    def _catalog(self) -> dict:
        def skill(slug, stars):
            return {
                "id": slug, "slug": slug, "name": slug, "github_stars": stars,
                "repo_url": f"https://github.com/ex/{slug}", "skill_path": "skills/x",
                "skill_md": "old", "author": "ex", "license": "MIT",
                "files_found": ["SKILL.md"],
            }
        return {"version": "1.1.0", "total": 4, "skills": [
            skill("zeta", 50), skill("rederive", 1), skill("quiet-skill", 9), skill("alpha", 1),
        ]}

    def test_ranked_views_then_unranked_stars(self):
        order = SF.order_skills(self._catalog()["skills"], {"rederive": 5000, "quiet-skill": 3})
        self.assertEqual([row["slug"] for row in order], ["rederive", "quiet-skill", "zeta", "alpha"])
        self.assertEqual(order[0]["tier"], "ranked")
        self.assertEqual(order[2]["tier"], "unranked")
        self.assertEqual(order[2]["github_stars"], 50)

    def test_ranking_csv_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "rank.csv"
            csv_path.write_text("slug,views\nrederive,5000\nquiet-skill,3\n", encoding="utf-8")
            self.assertEqual(SF.load_ranking(csv_path), {"rederive": 5000.0, "quiet-skill": 3.0})
            json_path = Path(tmp) / "rank.json"
            json_path.write_text(json.dumps([{"slug": "b", "views": 2}, {"slug": "a", "views": 9}]), encoding="utf-8")
            self.assertEqual(SF.load_ranking(json_path)["a"], 9.0)

    def _capture_factory(self, fail_slug: str | None = None, fails: int = 0):
        calls: list[str] = []
        remaining = {"n": fails}

        def capture(owner, repo, folder, ref, slug_box=calls):
            slug = repo
            slug_box.append(slug)
            if fail_slug and slug == fail_slug and remaining["n"] > 0:
                remaining["n"] -= 1
                return {"ok": False, "reason": "transient"}
            text = f"---\nname: {slug}\ndescription: hello {slug}\n---\n\nbody\n"
            script = f"echo {slug}\n"
            return {
                "ok": True, "truncated": False, "missing_skill_md": False, "readme": None,
                "skill_md": text,
                "files": [
                    {"path": "SKILL.md", "size": len(text.encode()), "sha": _sha(text), "content": text},
                    {"path": "scripts/run.sh", "size": len(script.encode()), "sha": _sha(script), "content": script},
                ],
            }
        return calls, capture

    def test_dry_run_order_and_diff_without_writing_state(self):
        calls, capture = self._capture_factory()
        with tempfile.TemporaryDirectory() as tmp:
            state = SF.BackfillState(Path(tmp) / "skill-files-backfill.json")
            result = SF.run_backfill(
                self._catalog(), {"rederive": 5000, "quiet-skill": 3},
                state=state, apply=False, batch=10, remaining=100,
                capture=capture, preview_slugs=["rederive"],
            )
            self.assertEqual([row["slug"] for row in result["order"]], ["rederive", "quiet-skill", "zeta", "alpha"])
            self.assertEqual(result["planned_this_run"], ["rederive", "quiet-skill", "zeta", "alpha"][:10])
            self.assertGreater(result["diff_bytes"], 0)
            self.assertEqual(result["updated"], ["rederive"])
            preview = result["results"][0]["files"]
            self.assertTrue(any(f["path"] == "scripts/run.sh" and f["content_present"] for f in preview))
            self.assertFalse(state.path.exists())
            self.assertEqual(calls, ["rederive"])

    def test_apply_is_serial_resumable_and_idempotent(self):
        calls, capture = self._capture_factory(fail_slug="quiet-skill", fails=1)
        catalog = self._catalog()
        ranking = {"rederive": 5000, "quiet-skill": 3}
        with tempfile.TemporaryDirectory() as tmp:
            state = SF.BackfillState(Path(tmp) / "state.json")
            first = SF.run_backfill(catalog, ranking, state=state, apply=True, batch=10, remaining=100, capture=capture)
            self.assertEqual(first["stopped"], "transient")
            self.assertEqual(first["updated"], ["rederive"])
            self.assertEqual(calls, ["rederive", "quiet-skill"])
            state.load()
            self.assertEqual(state.data["cursor"], 1)
            self.assertIn("rederive", state.data["pending"])
            # Killed before the PR: the on-disk catalog is still the original.
            second = SF.run_backfill(catalog, ranking, state=state, apply=True, batch=10, remaining=100, capture=capture)
            self.assertNotIn("rederive", calls[2:])
            self.assertEqual(calls[2:], ["quiet-skill", "zeta", "alpha"])
            slugs = [s["slug"] for s in second["catalog"]["skills"]]
            self.assertEqual(slugs, ["zeta", "rederive", "quiet-skill", "alpha"])
            by_slug = {s["slug"]: s for s in second["catalog"]["skills"]}
            for slug in ("rederive", "quiet-skill", "zeta", "alpha"):
                paths = [f["path"] for f in by_slug[slug]["files"]]
                self.assertIn("scripts/run.sh", paths)
                self.assertEqual(by_slug[slug]["license"], "MIT")
                self.assertEqual(by_slug[slug]["author"], "ex")
            state.clear_pending()
            third = SF.run_backfill(second["catalog"], ranking, state=state, apply=True, batch=10, remaining=100, capture=capture)
            self.assertEqual(third["diff_bytes"], 0)
            self.assertEqual(third["unchanged"], ["rederive", "quiet-skill", "zeta", "alpha"])
            self.assertEqual(SF._canon(third["catalog"]), SF._canon(second["catalog"]))

    def test_publish_batch_is_catalog_only_and_does_not_merge(self):
        commands: list[list[str]] = []

        class Proc:
            def __init__(self, stdout="", code=0):
                self.stdout, self.stderr, self.returncode = stdout, "", code

        def run(cmd, cwd):
            commands.append(list(cmd))
            if cmd[:3] == ["git", "status", "--porcelain"]:
                return Proc("")
            if cmd[:4] == ["git", "diff", "--cached", "--name-only"]:
                return Proc(CATALOG_REL + "\n")
            if cmd[:3] == ["gh", "pr", "create"]:
                self.assertNotIn("--merge", cmd)
                return Proc("https://github.com/ProSkillsMD/website/pull/1\n")
            return Proc("")

        with tempfile.TemporaryDirectory() as tmp:
            website = Path(tmp)
            (website / "public").mkdir()
            (website / CATALOG_REL).write_text("{}\n", encoding="utf-8")
            catalog = {"skills": [{"id": "rederive", "slug": "rederive"}], "total": 1}
            body = BF.catalog_pr_body(["rederive"], old_total=1, new_total=1, diff_bytes=120)
            self.assertIsNotNone(ADDED_RE.search(body))
            self.assertEqual(int(ADDED_RE.search(body).group(1)), 1)
            title = BF.catalog_pr_title("20261101-120000", 1, 1)
            self.assertIsNotNone(TITLE_RE.search(title))
            branch = f"{HEAD_PREFIX}20261101-120000"
            out = BF.push_catalog_pr(run, website, catalog, branch=branch, title=title, body=body)
            self.assertTrue(out["ok"])
            self.assertEqual(out["files"], [CATALOG_REL])
            self.assertFalse(out["merge_invoked"])
            self.assertFalse(any(cmd[:3] == ["gh", "pr", "merge"] for cmd in commands))
            written = json.loads((website / CATALOG_REL).read_text(encoding="utf-8"))
            self.assertEqual(written["skills"][0]["slug"], "rederive")

    def test_daily_budget_matches_publisher_cap(self):
        self.assertEqual(DAILY_CAP, 100)
        merged = [
            {"mergedAt": "2026-11-01T02:00:00Z", "body": '"added_count": 40', "title": "x", "headRefName": HEAD_PREFIX + "a"},
            {"mergedAt": "2026-10-31T02:00:00Z", "body": '"added_count": 90', "title": "old", "headRefName": HEAD_PREFIX + "b"},
        ]
        # 02:00Z is 08:00 Asia/Dhaka on the same calendar day.
        self.assertEqual(BF.budget_remaining(merged, cap=DAILY_CAP, today="2026-11-01"), 60)


class TestCatalogUpdateCapture(unittest.TestCase):
    def test_new_publish_records_helper_files(self):
        seen = {}

        def capture(owner, repo, folder, ref):
            seen["folder"] = folder
            text = SKILL
            return {
                "ok": True, "truncated": False, "skill_md": text, "readme": None,
                "files": [
                    {"path": "SKILL.md", "size": len(text.encode()), "sha": _sha(text), "content": text},
                    {"path": "scripts/history.sh", "size": len(HISTORY.encode()), "sha": _sha(HISTORY), "content": HISTORY},
                    {"path": "scripts/structure.py", "size": len(STRUCTURE.encode()), "sha": _sha(STRUCTURE), "content": STRUCTURE},
                ],
            }

        cand = {
            "issue": 7, "identity": "github:ericjujianzou/rederive::skills/rederive",
            "owner": "ericjujianzou", "repo": "rederive",
            "repo_url": "https://github.com/ericjujianzou/rederive",
            "subpath": "skills/rederive", "skill_path": "skills/rederive/SKILL.md",
            "default_branch": "main", "stars": 4, "license": "MIT", "attribution": "Eric Zou",
        }
        _staged, new, skips = plan_update({"skills": [], "total": 0}, [cand], offline=True, capture=capture)
        self.assertEqual(skips, [])
        self.assertEqual(seen["folder"], "skills/rederive")
        paths = [f["path"] for f in new[0]["files"]]
        self.assertEqual(paths, ["SKILL.md", "scripts/history.sh", "scripts/structure.py"])
        self.assertEqual(new[0]["files"][1]["content"], HISTORY)
        self.assertEqual(new[0]["license"], "MIT")
        self.assertEqual(new[0]["attribution"], "Eric Zou")
        self.assertEqual(new[0]["author"], "ericjujianzou")

    def test_policy_blocks_a_new_publish(self):
        def capture(owner, repo, folder, ref):
            text = "---\nname: pose\ndescription: Generate nude images\n---\n\n"
            return {"ok": True, "truncated": False, "skill_md": text, "files": [
                {"path": "SKILL.md", "size": len(text.encode()), "sha": _sha(text), "content": text},
            ]}

        cand = {
            "issue": 8, "identity": "github:acme/tools::skills/pose",
            "owner": "acme", "repo": "tools", "repo_url": "https://github.com/acme/tools",
            "subpath": "skills/pose", "skill_path": "skills/pose/SKILL.md", "default_branch": "main",
        }
        _staged, new, skips = plan_update({"skills": [], "total": 0}, [cand], offline=True, capture=capture)
        self.assertEqual(new, [])
        self.assertEqual(skips[0]["reason"], "content_policy")


class TestBackfillCli(unittest.TestCase):
    def test_dry_run_prints_order_and_diff_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            catalog = {
                "version": "1.1.0", "total": 3,
                "skills": [
                    {"id": "zeta", "slug": "zeta", "github_stars": 50, "repo_url": "https://github.com/ex/zeta",
                     "skill_path": "skills/z", "author": "ex", "license": "MIT"},
                    {"id": "rederive", "slug": "rederive", "github_stars": 1,
                     "repo_url": "https://github.com/ex/rederive", "skill_path": "skills/rederive",
                     "author": "ex", "license": "MIT"},
                    {"id": "alpha", "slug": "alpha", "github_stars": 2, "repo_url": "https://github.com/ex/alpha",
                     "skill_path": "skills/a", "author": "ex", "license": "MIT"},
                ],
            }
            cat_path = tmp_path / "skills-catalog.json"
            cat_path.write_text(catalog_json_dumps(catalog) + "\n", encoding="utf-8")
            rank = tmp_path / "rank.csv"
            rank.write_text("slug,views\nrederive,5000\n", encoding="utf-8")
            state = tmp_path / "state.json"
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "backfill_skill_files.py"),
                 "--catalog", str(cat_path), "--ranking", str(rank), "--state", str(state),
                 "--batch", "2"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["dry_run"])
            self.assertEqual([row["slug"] for row in payload["order"]], ["rederive", "zeta", "alpha"])
            self.assertEqual(payload["planned_this_run"], ["rederive", "zeta"])
            self.assertEqual(payload["diff_bytes"], 0)
            self.assertIn("diff_bytes=0", payload["message"])
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
