#!/usr/bin/env python3
"""Static text inspection of candidate files — NEVER execute candidate code."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    DEFAULT_DB_PATH,
    DEFAULT_POLICIES_PATH,
    DEFAULT_STATE_ROOT,
    PROJECT_ROOT,
    StateRootError,
    add_state_root_arg,
    connect_db,
    emit,
    ensure_schema,
    ensure_state_dirs,
    exit_fail,
    exit_ok,
    get_limits,
    get_state_paths,
    iso_now,
    load_policies,
    path_escapes_root,
    redact_secrets,
    resolve_under_root,
    upsert_stage_run,
    write_file_owner_only,
)

# (rule_name, severity, pattern) — text-only detection; never executed.
# Severity: low | medium | high | critical
FLAG_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("destructive_rm_rf", "critical", re.compile(r"(?i)\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force\s+)?.*(/|/\*|~)")),
    ("destructive_rm_rf_root", "critical", re.compile(r"(?i)\brm\s+-rf\s+/" )),
    ("destructive_mkfs", "critical", re.compile(r"(?i)\bmkfs(?:\.\w+)?\b")),
    ("destructive_dd_disk", "critical", re.compile(r"(?i)\bdd\s+.*\bif=|\bdd\s+.*\bof=/dev/")),
    ("fork_bomb", "critical", re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;?\s*:")),
    ("cred_etc_shadow", "critical", re.compile(r"(?i)/etc/shadow")),
    ("cred_ssh_id", "critical", re.compile(r"(?i)~/?\.ssh/id_|\.ssh/id_rsa|\.ssh/id_ed25519")),
    ("cred_aws_secret", "critical", re.compile(r"(?i)\bAWS_SECRET_ACCESS_KEY\b")),
    ("cred_github_token_env", "critical", re.compile(r"(?i)\bGITHUB_TOKEN\b|\benv.*GITHUB_TOKEN|printenv.*GITHUB")),
    ("download_curl_bash", "critical", re.compile(r"(?i)\bcurl\b[^|\n]*\|\s*(?:ba)?sh\b")),
    ("download_wget_sh", "critical", re.compile(r"(?i)\bwget\b[^|\n]*\|\s*(?:ba)?sh\b")),
    ("download_base64_sh", "critical", re.compile(r"(?i)base64\s+(?:-d|--decode)\s*\|?\s*(?:ba)?sh|base64\s+-d\s+\S+\s*\|\s*(?:ba)?sh")),
    ("download_and_run", "critical", re.compile(
        r"(?i)(?:curl|wget).*(?:\|\s*(?:ba)?sh|>\s*/tmp/.*(?:chmod|\./)|;\s*(?:ba)?sh\b)"
    )),
    ("persist_crontab", "critical", re.compile(r"(?i)\bcrontab\b")),
    ("persist_systemd", "critical", re.compile(r"(?i)systemd|/etc/systemd|\.service\b.*\[Unit\]")),
    ("persist_bashrc_curl", "critical", re.compile(r"(?i)(?:curl|wget).*(?:>>|>).*\.bashrc|~/?\.bashrc.*(?:curl|wget)|\.bashrc.*(?:curl|wget)")),
    ("eval_call", "high", re.compile(r"\beval\s*\(")),
    ("exec_call", "high", re.compile(r"\bexec\s*\(")),
    ("dunder_import", "high", re.compile(r"\b__import__\s*\(")),
    ("compile_call", "medium", re.compile(r"\bcompile\s*\(")),
    ("subprocess", "medium", re.compile(r"\bsubprocess\b")),
    ("os_system", "medium", re.compile(r"\bos\.system\s*\(")),
    ("socket", "low", re.compile(r"\bsocket\b")),
    ("crypto_miner", "high", re.compile(r"(?i)\b(?:xmrig|coinhive|cryptonight|minerd|nicehash)\b")),
    ("requests_pipe_shell", "critical", re.compile(r"(?i)\brequests\b.*(?:curl|bash)|\bcurl\b.*\|\s*(?:ba)?sh")),
]

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
BINARY_SNIFF_BYTES = 8192


def _is_probably_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    textish = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b <= 126)
    return (textish / len(sample)) < 0.70


def _max_severity(findings: list[dict]) -> str:
    if not findings:
        return "none"
    best = "low"
    for f in findings:
        sev = f.get("severity", "low")
        if SEVERITY_RANK.get(sev, 0) > SEVERITY_RANK.get(best, 0):
            best = sev
    return best


def scan_candidate(
    local_path: Path,
    limits: dict,
    *,
    project_root: Path | None = None,
) -> dict:
    """Walk candidate files and flag suspicious text patterns. Read-only.

    Never executes candidate content. Detects symlink escapes as critical.
    """
    root = (project_root or PROJECT_ROOT).resolve()
    max_files = int(limits.get("max_files_per_candidate", 200))
    max_bytes = int(limits.get("max_total_bytes_per_candidate", 5242880))
    candidate_root = local_path.resolve()

    findings: list[dict] = []
    files_scanned = 0
    bytes_read = 0
    skipped_binary = 0
    truncated = False
    symlink_escapes = 0

    for path in sorted(local_path.rglob("*")):
        try:
            rel_parts = path.relative_to(local_path).parts
        except ValueError:
            rel_parts = path.parts
        if any(part.startswith(".") or part == "__pycache__" for part in rel_parts):
            continue

        if path.is_symlink():
            try:
                rel = str(path.relative_to(local_path))
            except ValueError:
                rel = str(path)
            if path_escapes_root(path, candidate_root) or path_escapes_root(path, root):
                symlink_escapes += 1
                findings.append(
                    {
                        "rule": "symlink_escape",
                        "severity": "critical",
                        "file": rel,
                        "snippet": "[REDACTED symlink target outside allowed root]",
                    }
                )
            continue

        if not path.is_file():
            continue

        if files_scanned >= max_files or bytes_read >= max_bytes:
            truncated = True
            break

        try:
            size = path.stat().st_size
        except OSError:
            continue

        try:
            with path.open("rb") as fh:
                sample = fh.read(BINARY_SNIFF_BYTES)
        except OSError:
            continue

        if _is_probably_binary(sample):
            skipped_binary += 1
            files_scanned += 1
            bytes_read += min(size, BINARY_SNIFF_BYTES)
            continue

        remaining = max_bytes - bytes_read
        try:
            raw = path.read_bytes()[:remaining]
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            continue

        files_scanned += 1
        bytes_read += len(raw)

        try:
            rel = str(path.relative_to(local_path))
        except ValueError:
            rel = str(path)

        for name, severity, pattern in FLAG_PATTERNS:
            for match in pattern.finditer(text):
                start = max(0, match.start() - 40)
                end = min(len(text), match.end() + 40)
                snippet = redact_secrets(text[start:end].replace("\n", " "))
                findings.append(
                    {
                        "rule": name,
                        "severity": severity,
                        "file": rel,
                        "snippet": snippet,
                    }
                )

    max_sev = _max_severity(findings)
    has_critical = max_sev == "critical"
    risk = max_sev if findings else "low"
    ok = len(findings) == 0
    return {
        "ok": ok,
        "risk": risk,
        "max_severity": max_sev,
        "has_critical": has_critical,
        "files_scanned": files_scanned,
        "bytes_read": bytes_read,
        "skipped_binary": skipped_binary,
        "symlink_escapes": symlink_escapes,
        "truncated": truncated,
        "finding_count": len(findings),
        "findings": findings[:50],
    }


def run_static_scan(
    event_id: str,
    *,
    db: Path | str = DEFAULT_DB_PATH,
    policies_path: Path | str = DEFAULT_POLICIES_PATH,
    state_root: Path | str | None = None,
    dry_run: bool = True,
    emit_output: bool = True,
) -> dict[str, Any]:
    """Run static scan for an event. Returns result dict; never executes code."""
    event_id = event_id.strip()
    db_path = Path(db)
    pol_path = Path(policies_path)
    try:
        paths = get_state_paths(
            state_root if state_root is not None else DEFAULT_STATE_ROOT
        )
    except StateRootError as exc:
        result = {
            "stage": "static_scan",
            "status": "error",
            "dry_run": dry_run,
            "message": str(exc),
            "event_id": event_id,
        }
        if emit_output:
            emit(
                stage="static_scan",
                status="error",
                dry_run=dry_run,
                message=str(exc),
                event_id=event_id,
            )
        return result

    if not event_id:
        result = {
            "stage": "static_scan",
            "status": "error",
            "dry_run": dry_run,
            "message": "empty event_id",
            "event_id": event_id,
        }
        if emit_output:
            emit(**{k: v for k, v in result.items() if k != "event_id"}, event_id=event_id)
        return result

    policies = load_policies(pol_path) if pol_path.exists() else {}
    limits = get_limits(policies)

    if not db_path.exists():
        result = {
            "stage": "static_scan",
            "status": "error",
            "dry_run": dry_run,
            "message": f"database not found: {db_path}",
            "event_id": event_id,
        }
        if emit_output:
            emit(
                stage="static_scan",
                status="error",
                dry_run=dry_run,
                message=result["message"],
                event_id=event_id,
            )
        return result

    conn = connect_db(db_path)
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT event_id, name, local_path FROM candidates WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if not row or not row["local_path"]:
            result = {
                "stage": "static_scan",
                "status": "error",
                "dry_run": dry_run,
                "message": f"no candidate/local_path for event_id={event_id}",
                "event_id": event_id,
            }
            if emit_output:
                emit(
                    stage="static_scan",
                    status="error",
                    dry_run=dry_run,
                    message=result["message"],
                    event_id=event_id,
                )
            return result

        try:
            local_path = resolve_under_root(PROJECT_ROOT, row["local_path"])
        except ValueError as exc:
            result = {
                "stage": "static_scan",
                "status": "error",
                "dry_run": dry_run,
                "message": str(exc),
                "event_id": event_id,
            }
            if emit_output:
                emit(
                    stage="static_scan",
                    status="error",
                    dry_run=dry_run,
                    message=str(exc),
                    event_id=event_id,
                )
            return result

        if not local_path.is_dir():
            result = {
                "stage": "static_scan",
                "status": "error",
                "dry_run": dry_run,
                "message": f"local_path is not a directory: {local_path}",
                "event_id": event_id,
            }
            if emit_output:
                emit(
                    stage="static_scan",
                    status="error",
                    dry_run=dry_run,
                    message=result["message"],
                    event_id=event_id,
                )
            return result

        scan = scan_candidate(local_path, limits)
        if scan["has_critical"]:
            status = "critical"
        elif scan["ok"]:
            status = "ok"
        else:
            status = "flagged"
        summary = json.dumps(scan, ensure_ascii=False)

        if dry_run:
            result = {
                "stage": "static_scan",
                "status": status,
                "dry_run": True,
                "message": "Static scan complete (dry-run; no DB/artifact write)",
                "event_id": event_id,
                "item": row["name"],
                "summary": scan,
                "action_required": not scan["ok"],
                "has_critical": scan["has_critical"],
            }
            if emit_output:
                emit(
                    stage="static_scan",
                    status=status,
                    dry_run=True,
                    message=result["message"],
                    event_id=event_id,
                    item=row["name"],
                    summary=scan,
                    action_required=not scan["ok"],
                    has_critical=scan["has_critical"],
                )
            return result

        upsert_stage_run(conn, event_id, "static_scan", status, summary=summary)

        ensure_state_dirs(paths)
        art_name = f"{event_id}-static_scan.json"
        art_path = paths.artifacts_dir / art_name
        redacted_body = redact_secrets(summary)
        write_file_owner_only(
            art_path, redacted_body + "\n", state_root=paths.state_root
        )
        sha = hashlib.sha256(redacted_body.encode("utf-8")).hexdigest()
        art_rel = str(art_path.relative_to(paths.state_root))
        conn.execute(
            """
            INSERT INTO artifacts (event_id, stage, kind, path, sha256, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                "static_scan",
                "scan_report",
                art_rel,
                sha,
                redacted_body,
                iso_now(),
            ),
        )
        if scan["has_critical"]:
            cand_status = "quarantined"
        elif scan["ok"]:
            cand_status = "scanned"
        else:
            cand_status = "scan_flagged"
        conn.execute(
            "UPDATE candidates SET status = ? WHERE event_id = ?",
            (cand_status, event_id),
        )
        conn.commit()
        result = {
            "stage": "static_scan",
            "status": status,
            "dry_run": False,
            "message": "Static scan recorded",
            "event_id": event_id,
            "item": row["name"],
            "summary": scan,
            "artifact": art_rel,
            "action_required": not scan["ok"],
            "has_critical": scan["has_critical"],
            "candidate_status": cand_status,
        }
        if emit_output:
            emit(
                stage="static_scan",
                status=status,
                dry_run=False,
                message=result["message"],
                event_id=event_id,
                item=row["name"],
                summary=scan,
                artifact=result["artifact"],
                action_required=not scan["ok"],
                has_critical=scan["has_critical"],
                candidate_status=cand_status,
            )
        return result
    finally:
        conn.close()


def main() -> None:
    from _common import add_dry_run_apply_flags, resolve_dry_run

    parser = argparse.ArgumentParser(description="Static text scan of candidate (no code exec).")
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--policies", type=Path, default=DEFAULT_POLICIES_PATH)
    add_state_root_arg(parser)
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()

    result = run_static_scan(
        args.event_id,
        db=args.db,
        policies_path=args.policies,
        state_root=args.state_root,
        dry_run=resolve_dry_run(args),
        emit_output=True,
    )
    if result.get("status") == "error":
        exit_fail(2)
    exit_ok()


if __name__ == "__main__":
    main()
