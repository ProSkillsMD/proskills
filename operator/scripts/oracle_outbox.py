#!/usr/bin/env python3
"""Queue structured Oracle events into outbound_events (validated, idempotent).

Dry-run by default; --apply writes. Never accepts tokens as CLI args.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from ipaddress import ip_address, ip_network
from typing import Any
from urllib.parse import urlparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    DEFAULT_DB_PATH,
    DEFAULT_POLICIES_PATH,
    add_dry_run_apply_flags,
    connect_db,
    contains_secret_like,
    emit_json,
    ensure_schema,
    exit_fail,
    exit_ok,
    get_oracle_delivery,
    iso_now,
    load_policies,
    redact_secrets,
    resolve_dry_run,
)

ALLOWED_STAGES = frozenset(
    {
        "intake",
        "screen",
        "test",
        "score",
        "pr",
        "deploy",
        "maintenance",
        "validate",
        "static_scan",
        "report",
        "pipeline",
        "purge",
        "discover",
        "sandbox_test",
        "catalog_update",
        "create_pr",
        "reconcile",
        "init_state",
    }
)

ALLOWED_STATUSES = frozenset(
    {
        "started",
        "needs_approval",
        "completed",
        "failed",
        "ok",
        "quarantined",
        "rejected",
        "duplicate",
        "blocked",
        "flagged",
        "critical",
        "error",
        "pending",
        "delivered",
    }
)

REQUIRED_FIELDS = (
    "project",
    "event_id",
    "stage",
    "status",
    "item",
    "summary",
    "evidence_urls",
    "action_required",
    "timestamp",
)

MAX_SUMMARY = 2000
MAX_ITEM = 512
MAX_EVENT_ID = 256
MAX_URLS = 20
MAX_URL_LEN = 2048

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LOCAL_PATH_RE = re.compile(
    r"(?i)(?:^|[\s\"'=])(?:file://|/home/|/Users/|/var/|/tmp/|/etc/|[A-Za-z]:\\|\.\./)"
)

_PRIVATE_NETS = [
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
    ip_network("fe80::/10"),
]


class PayloadValidationError(ValueError):
    """Raised when Oracle payload fails validation."""


def _reject_control(s: str, field: str) -> None:
    if _CONTROL_RE.search(s):
        raise PayloadValidationError(f"{field}: control characters not allowed")


def _reject_local_paths(s: str, field: str) -> None:
    if _LOCAL_PATH_RE.search(s):
        raise PayloadValidationError(f"{field}: local paths not allowed")


def _reject_secrets(s: str, field: str) -> None:
    if contains_secret_like(s):
        raise PayloadValidationError(
            f"{field}: secret-like value rejected (would be redacted)"
        )


def _is_private_host(host: str) -> bool:
    h = host.strip("[]").lower()
    if h in ("localhost",) or h.endswith(".local"):
        return True
    try:
        addr = ip_address(h)
    except ValueError:
        return False
    return any(addr in net for net in _PRIVATE_NETS)


def validate_evidence_url(url: str) -> None:
    """Reject non-https, credentials-in-URL, private/link-local, localhost, .local."""
    if not isinstance(url, str) or not url:
        raise PayloadValidationError("evidence_urls: empty URL")
    if len(url) > MAX_URL_LEN:
        raise PayloadValidationError("evidence_urls: URL too long")
    _reject_control(url, "evidence_urls")
    _reject_secrets(url, "evidence_urls")
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise PayloadValidationError(
            "evidence_urls: only https:// URLs allowed (no http/file/other)"
        )
    if parsed.username or parsed.password:
        raise PayloadValidationError("evidence_urls: credentials in URL rejected")
    host = parsed.hostname or ""
    if not host:
        raise PayloadValidationError("evidence_urls: missing hostname")
    if _is_private_host(host):
        raise PayloadValidationError(
            "evidence_urls: private/link-local/localhost/.local host rejected"
        )
    if "@" in (parsed.netloc or ""):
        raise PayloadValidationError("evidence_urls: credentials in URL rejected")


def validate_oracle_payload(payload: dict[str, Any], *, expected_project: str) -> dict[str, Any]:
    """Validate and return a sanitized copy. Reject extras and invalid values."""
    if not isinstance(payload, dict):
        raise PayloadValidationError("payload must be an object")

    extras = set(payload.keys()) - set(REQUIRED_FIELDS)
    if extras:
        raise PayloadValidationError(f"unknown fields: {sorted(extras)}")

    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    if missing:
        raise PayloadValidationError(f"missing fields: {missing}")

    project = payload["project"]
    if not isinstance(project, str) or project != expected_project:
        raise PayloadValidationError(
            f"project must be {expected_project!r}, got {project!r}"
        )

    event_id = payload["event_id"]
    if not isinstance(event_id, str) or not event_id.strip():
        raise PayloadValidationError("event_id must be non-empty string")
    if len(event_id) > MAX_EVENT_ID:
        raise PayloadValidationError("event_id too long")
    _reject_control(event_id, "event_id")
    _reject_secrets(event_id, "event_id")

    stage = payload["stage"]
    if stage not in ALLOWED_STAGES:
        raise PayloadValidationError(f"stage not allowed: {stage!r}")

    status = payload["status"]
    if status not in ALLOWED_STATUSES:
        raise PayloadValidationError(f"status not allowed: {status!r}")

    item = payload["item"]
    if not isinstance(item, str):
        raise PayloadValidationError("item must be string")
    if len(item) > MAX_ITEM:
        raise PayloadValidationError("item too long")
    _reject_control(item, "item")
    _reject_local_paths(item, "item")
    _reject_secrets(item, "item")

    summary = payload["summary"]
    if not isinstance(summary, str):
        raise PayloadValidationError("summary must be string")
    if len(summary) > MAX_SUMMARY:
        raise PayloadValidationError(f"summary exceeds max length {MAX_SUMMARY}")
    _reject_control(summary, "summary")
    _reject_local_paths(summary, "summary")
    _reject_secrets(summary, "summary")

    urls = payload["evidence_urls"]
    if not isinstance(urls, list):
        raise PayloadValidationError("evidence_urls must be a list")
    if len(urls) > MAX_URLS:
        raise PayloadValidationError(f"evidence_urls exceeds max {MAX_URLS}")
    for u in urls:
        validate_evidence_url(u)

    action_required = payload["action_required"]
    if not isinstance(action_required, bool):
        raise PayloadValidationError("action_required must be bool")

    timestamp = payload["timestamp"]
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise PayloadValidationError("timestamp must be ISO-8601 with Z suffix")
    _reject_control(timestamp, "timestamp")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", timestamp):
        raise PayloadValidationError("timestamp must be ISO-8601 Z form")

    return {
        "project": project,
        "event_id": event_id.strip(),
        "stage": stage,
        "status": status,
        "item": item,
        "summary": summary,
        "evidence_urls": list(urls),
        "action_required": action_required,
        "timestamp": timestamp,
    }


def make_outbound_key(
    project: str, event_id: str, stage: str, status: str, item: str
) -> str:
    """Deterministic outbound_key: sha256(project|event_id|stage|status|item)."""
    material = f"{project}|{event_id}|{stage}|{status}|{item}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def ensure_event_row(conn, event_id: str) -> None:
    """Ensure a parent events row exists for FK (minimal stub if missing)."""
    row = conn.execute(
        "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row:
        return
    conn.execute(
        """
        INSERT INTO events (event_id, source, payload_json, received_at, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_id, "oracle_outbox", None, iso_now(), "outbound_only"),
    )


def run_outbox(
    payload: dict[str, Any],
    *,
    db: Path | str = DEFAULT_DB_PATH,
    policies_path: Path | str = DEFAULT_POLICIES_PATH,
    outbound_key: str | None = None,
    dry_run: bool = True,
    emit_output: bool = True,
) -> dict[str, Any]:
    """Validate and optionally enqueue an Oracle outbound event."""
    pol_path = Path(policies_path)
    policies = load_policies(pol_path) if pol_path.exists() else {}
    delivery = get_oracle_delivery(policies)
    expected_project = str(delivery.get("project") or "proskills-md")

    try:
        clean = validate_oracle_payload(payload, expected_project=expected_project)
    except PayloadValidationError as exc:
        result = {
            "stage": "oracle_outbox",
            "status": "rejected",
            "dry_run": dry_run,
            "message": redact_secrets(str(exc)),
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(result)
        return result

    key = outbound_key or make_outbound_key(
        clean["project"],
        clean["event_id"],
        clean["stage"],
        clean["status"],
        clean["item"],
    )
    if outbound_key is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{16,128}", outbound_key):
            if contains_secret_like(outbound_key) or _CONTROL_RE.search(outbound_key):
                result = {
                    "stage": "oracle_outbox",
                    "status": "rejected",
                    "dry_run": dry_run,
                    "message": "outbound_key rejected",
                    "timestamp": iso_now(),
                }
                if emit_output:
                    emit_json(result)
                return result

    body_json = json.dumps(clean, ensure_ascii=False, sort_keys=True)

    if dry_run:
        result = {
            "stage": "oracle_outbox",
            "status": "ok",
            "dry_run": True,
            "message": "Would enqueue outbound event (no DB write)",
            "outbound_key": key,
            "event_id": clean["event_id"],
            "payload": clean,
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(result)
        return result

    db_path = Path(db)
    conn = connect_db(db_path)
    try:
        ensure_schema(conn)
        ensure_event_row(conn, clean["event_id"])
        existing = conn.execute(
            "SELECT id, status, outbound_key FROM outbound_events WHERE outbound_key = ?",
            (key,),
        ).fetchone()
        if existing:
            conn.commit()
            result = {
                "stage": "oracle_outbox",
                "status": "duplicate",
                "dry_run": False,
                "message": "Outbound key already queued; ack without second row",
                "outbound_key": key,
                "event_id": clean["event_id"],
                "existing_status": existing["status"],
                "timestamp": iso_now(),
            }
            if emit_output:
                emit_json(result)
            return result

        now = iso_now()
        conn.execute(
            """
            INSERT INTO outbound_events (
                event_id, outbound_key, stage, status, body_json,
                created_at, delivered_at, attempt_count, next_attempt_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?)
            """,
            (
                clean["event_id"],
                key,
                clean["stage"],
                "pending",
                body_json,
                now,
                now,
            ),
        )
        conn.commit()
        result = {
            "stage": "oracle_outbox",
            "status": "ok",
            "dry_run": False,
            "message": "Outbound event enqueued",
            "outbound_key": key,
            "event_id": clean["event_id"],
            "queue_status": "pending",
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(result)
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Queue validated Oracle events into outbound_events (dry-run default)."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--policies", type=Path, default=DEFAULT_POLICIES_PATH)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--item", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument(
        "--evidence-url",
        action="append",
        default=[],
        dest="evidence_urls",
        help="HTTPS evidence URL (repeatable)",
    )
    parser.add_argument(
        "--action-required",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--no-action-required",
        action="store_true",
        default=False,
        help="Explicitly set action_required=false",
    )
    parser.add_argument("--timestamp", default=None, help="ISO-8601 Z; default now")
    parser.add_argument("--project", default=None, help="Override project (must match policy)")
    parser.add_argument("--outbound-key", default=None, help="Explicit outbound_key")
    parser.add_argument(
        "--payload-json",
        default=None,
        help="Full JSON payload object (overrides individual fields when set)",
    )
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()

    policies = load_policies(args.policies) if Path(args.policies).exists() else {}
    delivery = get_oracle_delivery(policies)
    project = args.project or delivery.get("project") or "proskills-md"

    if args.payload_json:
        try:
            payload = json.loads(args.payload_json)
        except json.JSONDecodeError as exc:
            emit_json(
                {
                    "stage": "oracle_outbox",
                    "status": "rejected",
                    "dry_run": resolve_dry_run(args),
                    "message": f"invalid payload-json: {exc}",
                    "timestamp": iso_now(),
                }
            )
            exit_fail(2)
            return
    else:
        action_required = bool(args.action_required) and not args.no_action_required
        payload = {
            "project": project,
            "event_id": args.event_id,
            "stage": args.stage,
            "status": args.status,
            "item": args.item,
            "summary": args.summary,
            "evidence_urls": list(args.evidence_urls or []),
            "action_required": action_required,
            "timestamp": args.timestamp or iso_now(),
        }

    result = run_outbox(
        payload,
        db=args.db,
        policies_path=args.policies,
        outbound_key=args.outbound_key,
        dry_run=resolve_dry_run(args),
        emit_output=True,
    )
    if result.get("status") in ("rejected", "error"):
        exit_fail(2)
    exit_ok()


if __name__ == "__main__":
    main()
