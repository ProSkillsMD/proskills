#!/usr/bin/env python3
"""Deliver pending outbound_events to Oracle over stdlib HTTPS.

Reads ORACLE_ENDPOINT_URL and ORACLE_BEARER_TOKEN from environment ONLY.
Never prints tokens. Dry-run by default; --apply performs delivery.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    DEFAULT_DB_PATH,
    DEFAULT_POLICIES_PATH,
    add_dry_run_apply_flags,
    assert_insecure_for_tests_allowed,
    connect_db,
    emit_json,
    ensure_schema,
    exit_fail,
    exit_ok,
    get_oracle_delivery,
    is_loopback_host,
    iso_now,
    load_policies,
    redact_secrets,
    resolve_dry_run,
)


class DeliveryError(Exception):
    """Delivery failed (safe message; secrets redacted)."""


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_ack_sig(outbound_key: str, ack_secret: str) -> str:
    """HMAC-SHA256 hex of outbound_key with ACK secret."""
    return hmac.new(
        ack_secret.encode("utf-8"),
        outbound_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_ack(ack: dict[str, Any], outbound_key: str, ack_secret: str) -> bool:
    """Validate ack JSON: matching outbound_key + status accepted + valid sig."""
    if not isinstance(ack, dict):
        return False
    if ack.get("outbound_key") != outbound_key:
        return False
    if ack.get("status") != "accepted":
        return False
    sig = ack.get("sig")
    if not isinstance(sig, str) or not sig:
        return False
    expected = compute_ack_sig(outbound_key, ack_secret)
    return hmac.compare_digest(sig, expected)


def validate_endpoint_url(url: str, allowed_hostnames: list[str], *, require_https: bool = True) -> None:
    """Require HTTPS and hostname in policy allowlist."""
    parsed = urlparse(url)
    if require_https and parsed.scheme.lower() != "https":
        raise DeliveryError("ORACLE_ENDPOINT_URL must be https://")
    host = (parsed.hostname or "").lower()
    allowed = {h.lower() for h in allowed_hostnames}
    if host not in allowed:
        raise DeliveryError(
            f"endpoint hostname {host!r} not in oracle.delivery.allowed_hostnames"
        )
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise DeliveryError("endpoint URL must not contain credentials")


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    """Fail closed on redirects — do not follow."""

    def http_response(self, request, response):  # type: ignore[no-untyped-def]
        code = getattr(response, "status", None) or response.getcode()
        if 300 <= int(code) < 400:
            raise DeliveryError(f"redirect not allowed (HTTP {code})")
        return response

    https_response = http_response


def post_oracle(
    *,
    endpoint_url: str,
    bearer_token: str,
    body: dict[str, Any],
    outbound_key: str,
    connect_timeout: float,
    read_timeout: float,
    cafile: str | None = None,
    insecure_for_tests: bool = False,
) -> dict[str, Any]:
    """POST JSON body to Oracle HTTPS endpoint. Never logs token."""
    data = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    req = urllib.request.Request(
        endpoint_url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {bearer_token}",
            "X-Outbound-Key": outbound_key,
            "Accept": "application/json",
        },
    )
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    if insecure_for_tests:
        assert_insecure_for_tests_allowed(endpoint_url)
        host = urlparse(endpoint_url).hostname
        if not is_loopback_host(host):
            raise DeliveryError(
                f"--insecure-for-tests rejects non-loopback host: {host!r}"
            )
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        _NoRedirect,
    )
    timeout = max(connect_timeout, 0.1) + max(read_timeout, 0.1)
    timeout = min(timeout, connect_timeout + read_timeout)
    try:
        with opener.open(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read(1_000_000)
    except urllib.error.HTTPError as exc:
        try:
            _ = exc.read(4096)
        except Exception:
            pass
        raise DeliveryError(f"HTTP error {exc.code}") from None
    except urllib.error.URLError as exc:
        msg = redact_secrets(str(exc.reason if hasattr(exc, "reason") else exc))
        raise DeliveryError(f"URL error: {msg}") from None
    except DeliveryError:
        raise
    except Exception as exc:
        raise DeliveryError(redact_secrets(f"request failed: {exc}")) from None

    if int(code) != 200:
        raise DeliveryError(f"expected HTTP 200, got {code}")
    try:
        ack = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeliveryError("ack is not valid JSON") from exc
    return ack


def backoff_seconds(attempt_count: int, base: float, maximum: float) -> float:
    """Bounded exponential backoff: min(max, base * 2^attempt)."""
    delay = base * (2 ** max(0, attempt_count))
    return float(min(maximum, delay))


def run_delivery(
    *,
    db: Path | str = DEFAULT_DB_PATH,
    policies_path: Path | str = DEFAULT_POLICIES_PATH,
    outbound_key: str | None = None,
    dry_run: bool = True,
    emit_output: bool = True,
    cafile: str | None = None,
    insecure_for_tests: bool = False,
    endpoint_url_override: str | None = None,
) -> dict[str, Any]:
    """Deliver one or more pending outbound events. Env-only credentials."""
    pol_path = Path(policies_path)
    policies = load_policies(pol_path) if pol_path.exists() else {}
    delivery_pol = get_oracle_delivery(policies)
    allowed = list(delivery_pol.get("allowed_hostnames") or [])
    connect_t = float(delivery_pol.get("connect_timeout_sec", 3))
    read_t = float(delivery_pol.get("read_timeout_sec", 5))
    max_retries = int(delivery_pol.get("max_retries", 5))
    backoff_base = float(delivery_pol.get("backoff_base_sec", 1))
    backoff_max = float(delivery_pol.get("backoff_max_sec", 60))
    require_https = bool(delivery_pol.get("require_https", True))

    endpoint = endpoint_url_override or os.environ.get("ORACLE_ENDPOINT_URL", "")
    token = os.environ.get("ORACLE_BEARER_TOKEN", "")
    ack_secret = os.environ.get("ORACLE_ACK_SECRET", "")

    if insecure_for_tests:
        try:
            if os.environ.get("PROSKILLS_ENV") != "test":
                raise RuntimeError(
                    "--insecure-for-tests is only allowed when PROSKILLS_ENV=test"
                )
            if endpoint:
                assert_insecure_for_tests_allowed(endpoint)
        except RuntimeError as exc:
            result = {
                "stage": "oracle_delivery",
                "status": "error",
                "dry_run": dry_run,
                "message": str(exc),
                "timestamp": iso_now(),
            }
            if emit_output:
                emit_json(result)
            return result

    if not endpoint:
        result = {
            "stage": "oracle_delivery",
            "status": "error",
            "dry_run": dry_run,
            "message": "ORACLE_ENDPOINT_URL not set in environment",
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(result)
        return result

    try:
        validate_endpoint_url(endpoint, allowed, require_https=require_https)
    except DeliveryError as exc:
        result = {
            "stage": "oracle_delivery",
            "status": "error",
            "dry_run": dry_run,
            "message": str(exc),
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(result)
        return result

    if dry_run:
        result = {
            "stage": "oracle_delivery",
            "status": "ok",
            "dry_run": True,
            "message": "Would deliver pending outbound events (no network, no DB write)",
            "endpoint_host": urlparse(endpoint).hostname,
            "outbound_key_filter": outbound_key,
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(result)
        return result

    if not token:
        result = {
            "stage": "oracle_delivery",
            "status": "error",
            "dry_run": False,
            "message": "ORACLE_BEARER_TOKEN not set in environment",
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(result)
        return result
    if not ack_secret:
        result = {
            "stage": "oracle_delivery",
            "status": "error",
            "dry_run": False,
            "message": "ORACLE_ACK_SECRET not set in environment",
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(result)
        return result

    db_path = Path(db)
    if not db_path.exists():
        result = {
            "stage": "oracle_delivery",
            "status": "error",
            "dry_run": False,
            "message": f"database not found: {db_path}",
            "timestamp": iso_now(),
        }
        if emit_output:
            emit_json(result)
        return result

    now = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    conn = connect_db(db_path)
    try:
        ensure_schema(conn)
        if outbound_key:
            rows = conn.execute(
                "SELECT * FROM outbound_events WHERE outbound_key = ?",
                (outbound_key,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM outbound_events
                WHERE status IN ('pending', 'failed')
                ORDER BY id ASC
                """
            ).fetchall()

        for row in rows:
            key = row["outbound_key"]
            status = row["status"]
            attempt_count = int(row["attempt_count"] or 0)

            if status == "delivered":
                results.append(
                    {
                        "outbound_key": key,
                        "status": "already_delivered",
                        "message": "already delivered; no-op (no POST)",
                    }
                )
                continue

            next_at = _parse_iso(row["next_attempt_at"])
            if next_at and next_at > now:
                results.append(
                    {
                        "outbound_key": key,
                        "status": "deferred",
                        "message": "next_attempt_at not reached",
                        "next_attempt_at": row["next_attempt_at"],
                    }
                )
                continue

            if attempt_count >= max_retries:
                conn.execute(
                    "UPDATE outbound_events SET status = ? WHERE outbound_key = ?",
                    ("failed", key),
                )
                results.append(
                    {
                        "outbound_key": key,
                        "status": "failed",
                        "message": "max_retries exceeded",
                        "attempt_count": attempt_count,
                    }
                )
                continue

            try:
                body = json.loads(row["body_json"] or "{}")
            except json.JSONDecodeError:
                body = {}
            post_body = dict(body)
            post_body["outbound_key"] = key

            try:
                ack = post_oracle(
                    endpoint_url=endpoint,
                    bearer_token=token,
                    body=post_body,
                    outbound_key=key,
                    connect_timeout=connect_t,
                    read_timeout=read_t,
                    cafile=cafile,
                    insecure_for_tests=insecure_for_tests,
                )
                if not verify_ack(ack, key, ack_secret):
                    raise DeliveryError("ack signature/outbound_key validation failed")

                delivered_at = iso_now()
                conn.execute(
                    """
                    UPDATE outbound_events
                    SET status = ?, delivered_at = ?, attempt_count = ?, next_attempt_at = NULL
                    WHERE outbound_key = ?
                    """,
                    ("delivered", delivered_at, attempt_count + 1, key),
                )
                results.append(
                    {
                        "outbound_key": key,
                        "status": "delivered",
                        "delivered_at": delivered_at,
                        "attempt_count": attempt_count + 1,
                    }
                )
            except DeliveryError as exc:
                new_attempts = attempt_count + 1
                delay = backoff_seconds(new_attempts, backoff_base, backoff_max)
                next_attempt = (now + timedelta(seconds=delay)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                new_status = "failed" if new_attempts >= max_retries else "pending"
                conn.execute(
                    """
                    UPDATE outbound_events
                    SET status = ?, attempt_count = ?, next_attempt_at = ?
                    WHERE outbound_key = ?
                    """,
                    (new_status, new_attempts, next_attempt, key),
                )
                results.append(
                    {
                        "outbound_key": key,
                        "status": new_status,
                        "message": redact_secrets(str(exc)),
                        "attempt_count": new_attempts,
                        "next_attempt_at": next_attempt,
                    }
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    overall = "ok"
    if any(r.get("status") in ("failed", "error") for r in results):
        overall = "failed"
    elif any(r.get("status") == "delivered" for r in results):
        overall = "ok"
    elif any(r.get("status") == "already_delivered" for r in results) and results:
        overall = "already_delivered"
    elif not results:
        overall = "ok"

    result = {
        "stage": "oracle_delivery",
        "status": overall,
        "dry_run": False,
        "message": "Delivery pass complete",
        "results": results,
        "delivered_count": sum(1 for r in results if r.get("status") == "delivered"),
        "already_delivered_count": sum(
            1 for r in results if r.get("status") == "already_delivered"
        ),
        "timestamp": iso_now(),
    }
    if emit_output:
        emit_json(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deliver outbound_events to Oracle HTTPS (dry-run default; env credentials only)."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--policies", type=Path, default=DEFAULT_POLICIES_PATH)
    parser.add_argument("--outbound-key", default=None, help="Deliver a specific key only")
    parser.add_argument(
        "--cafile",
        default=None,
        help="Optional CA bundle path (tests: mock self-signed cert)",
    )
    parser.add_argument(
        "--insecure-for-tests",
        action="store_true",
        default=False,
        help="Disable TLS verify (requires PROSKILLS_ENV=test; loopback only)",
    )
    add_dry_run_apply_flags(parser)
    args = parser.parse_args()

    result = run_delivery(
        db=args.db,
        policies_path=args.policies,
        outbound_key=args.outbound_key,
        dry_run=resolve_dry_run(args),
        emit_output=True,
        cafile=args.cafile,
        insecure_for_tests=args.insecure_for_tests,
    )
    if result.get("status") in ("error", "failed"):
        exit_fail(2)
    exit_ok()


if __name__ == "__main__":
    main()
