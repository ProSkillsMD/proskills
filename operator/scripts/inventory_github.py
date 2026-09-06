#!/usr/bin/env python3
"""Build a static GitHub org inventory file for operator reference.

Preferred path: --from-json (merge known facts) and/or reading local shallow
clones under repos/ (read-only: .github layout only). Optional subprocess `gh`
ONLY if authenticated AND --use-gh is passed; otherwise skip network and
document the skip. Never prints tokens. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    PROJECT_ROOT,
    add_dry_run_apply_flags,
    emit_json,
    exit_fail,
    exit_ok,
    iso_now,
    redact_secrets,
    resolve_dry_run,
)

DEFAULT_OUT = PROJECT_ROOT / "inventory" / "inventory-2026-09-06.json"
DEFAULT_REPOS_ROOT = PROJECT_ROOT / "repos"


def _gh_auth_ok() -> tuple[bool, str]:
    """Return (ok, note). Never returns token material."""
    gh = shutil.which("gh")
    if not gh:
        return False, "gh binary not found on PATH"
    try:
        proc = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, redact_secrets(f"gh auth status failed: {exc}")
    # Redact any accidental secret-like content from status text
    combined = redact_secrets((proc.stdout or "") + "\n" + (proc.stderr or ""))
    if proc.returncode != 0:
        return False, "gh not authenticated (auth status non-zero)"
    # Do not echo full status (may contain account emails); keep short.
    logged_in = "Logged in" in combined or "Logged in to" in combined
    return True, "gh authenticated" if logged_in else "gh auth status ok"


def _scan_local_clone(repo_dir: Path) -> dict[str, Any]:
    """Read-only layout inventory of a shallow clone (no script execution)."""
    info: dict[str, Any] = {
        "local_path": str(repo_dir),
        "exists": repo_dir.is_dir(),
        "has_github_dir": False,
        "has_workflows_dir": False,
        "workflow_files": [],
        "issue_templates": [],
        "top_level_dirs": [],
        "top_level_files": [],
    }
    if not repo_dir.is_dir():
        return info
    try:
        for child in sorted(repo_dir.iterdir()):
            name = child.name
            if name == ".git":
                continue
            if child.is_dir():
                info["top_level_dirs"].append(name)
            elif child.is_file():
                info["top_level_files"].append(name)
    except OSError as exc:
        info["scan_error"] = redact_secrets(str(exc))
        return info

    gh_dir = repo_dir / ".github"
    info["has_github_dir"] = gh_dir.is_dir()
    workflows = gh_dir / "workflows"
    info["has_workflows_dir"] = workflows.is_dir()
    if workflows.is_dir():
        try:
            info["workflow_files"] = sorted(
                p.name for p in workflows.iterdir() if p.is_file()
            )
        except OSError:
            info["workflow_files"] = []
    templates = gh_dir / "ISSUE_TEMPLATE"
    if templates.is_dir():
        try:
            info["issue_templates"] = sorted(
                p.name for p in templates.iterdir() if p.is_file()
            )
        except OSError:
            info["issue_templates"] = []
    return info


def _merge_from_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("--from-json root must be a JSON object")
    return data


def build_inventory(
    *,
    repos_root: Path = DEFAULT_REPOS_ROOT,
    from_json: Path | None = None,
    use_gh: bool = False,
    known: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "generated_at": iso_now(),
        "generator": "scripts/inventory_github.py",
        "org": {
            "login": "ProSkillsMD",
            "notes": "Operator inventory; no secrets; PAT is personal Asif2BD not service account",
        },
        "summary": {
            "repos_known": 3,
            "proskills_open_issues_approx": 3184,
            "proskills_releases": 0,
            "actions_workflows_found": False,
            "github_app_inventory": "pending API",
            "auth_note": "GitHub MCP works as Asif2BD for org ProSkillsMD",
        },
        "labels_curio": [
            "curio:routed-to-rocket",
            "curio:routed-to-drax",
            "curio:routed-to-gamora",
            "rocket-pass",
            "drax-pass",
            "gamora-conditional",
            "submission",
            "auto-discovered",
        ],
        "repos": [
            {
                "name": "proskills",
                "full_name": "ProSkillsMD/proskills",
                "role": "catalog",
                "open_issues_approx": 3184,
                "releases": 0,
                "structure_expected": [
                    "listings/",
                    "skills/",
                    "reviews/",
                    "pending/",
                    "hosted/",
                    "index.json",
                    "_stats.json",
                    ".github/",
                ],
                "branches_note": "many branches including groot/publish/*",
                "notes": "Primary catalog; ~3184 open submission issues",
            },
            {
                "name": "avenger-initiative",
                "full_name": "ProSkillsMD/avenger-initiative",
                "role": "backup-skill",
                "notes": "Backup skill repo",
            },
            {
                "name": "skill-claude-code-cli",
                "full_name": "ProSkillsMD/skill-claude-code-cli",
                "role": "skill",
                "notes": "Claude Code CLI skill",
            },
        ],
        "network": {
            "use_gh_requested": bool(use_gh),
            "gh_used": False,
            "gh_status": "not attempted",
        },
        "local_clones": {},
    }

    if known:
        for key in ("org", "summary", "labels_curio", "repos", "decision_log"):
            if key in known:
                base[key] = known[key]
        if "generated_at" in known:
            base["source_generated_at"] = known["generated_at"]

    if from_json is not None:
        overlay = _merge_from_json(from_json)
        for key, value in overlay.items():
            if key == "network":
                continue
            base[key] = value

    local: dict[str, Any] = {}
    for name in ("proskills", "avenger-initiative", "skill-claude-code-cli"):
        local[name] = _scan_local_clone(repos_root / name)
    base["local_clones"] = local

    workflows_any = any(
        bool(v.get("has_workflows_dir")) for v in local.values() if isinstance(v, dict)
    )
    base["summary"] = dict(base.get("summary") or {})
    base["summary"]["actions_workflows_found"] = workflows_any
    base["summary"]["local_clone_scan"] = True

    by_name = {
        r.get("name"): r for r in base.get("repos", []) if isinstance(r, dict)
    }
    for name, scan in local.items():
        if name in by_name:
            by_name[name]["has_workflows_dir"] = scan.get("has_workflows_dir")
            by_name[name]["issue_templates"] = scan.get("issue_templates")
            by_name[name]["has_github_dir"] = scan.get("has_github_dir")

    if use_gh:
        ok, note = _gh_auth_ok()
        base["network"]["gh_status"] = note
        if not ok:
            base["network"]["gh_used"] = False
            base["network"]["skipped_reason"] = note
        else:
            base["network"]["gh_used"] = False
            base["network"]["skipped_reason"] = (
                "gh authenticated but inventory_github prefers static/"
                "--from-json facts; live issue enumeration not performed "
                "in this baseline pass"
            )
    else:
        base["network"]["gh_status"] = "skipped (pass --use-gh to attempt)"
        base["network"]["skipped_reason"] = "use_gh false; static + local clone only"

    return json.loads(redact_secrets(json.dumps(base, ensure_ascii=False)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Write static ProSkillsMD inventory JSON (local clones + --from-json; "
            "optional gh auth check only)."
        )
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--repos-root",
        type=Path,
        default=DEFAULT_REPOS_ROOT,
        help="Directory containing shallow clones",
    )
    parser.add_argument(
        "--from-json",
        type=Path,
        default=None,
        help="Merge known API/operator facts from a JSON object file",
    )
    parser.add_argument(
        "--use-gh",
        action="store_true",
        default=False,
        help="If set, check gh auth; still prefers static facts (no bulk fetch)",
    )
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()
    dry_run = resolve_dry_run(args)

    try:
        inventory = build_inventory(
            repos_root=args.repos_root,
            from_json=args.from_json,
            use_gh=bool(args.use_gh),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        emit_json(
            {
                "stage": "inventory_github",
                "status": "error",
                "dry_run": dry_run,
                "message": redact_secrets(str(exc)),
                "timestamp": iso_now(),
            }
        )
        exit_fail(2)
        return

    result: dict[str, Any] = {
        "stage": "inventory_github",
        "status": "ok",
        "dry_run": dry_run,
        "timestamp": iso_now(),
        "out": str(args.out),
        "summary": inventory.get("summary"),
        "network": inventory.get("network"),
        "message": "inventory built (dry-run; not written)"
        if dry_run
        else f"inventory written to {args.out}",
    }

    if not dry_run:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result["bytes"] = out_path.stat().st_size

    emit_json(result)
    exit_ok()


if __name__ == "__main__":
    main()
