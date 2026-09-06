"""Shared helpers for ProSkills operator stage CLIs (stdlib only)."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_ROOT = PROJECT_ROOT / "state"
DEFAULT_DB_PATH = DEFAULT_STATE_ROOT / "pipeline.sqlite3"
DEFAULT_POLICIES_PATH = PROJECT_ROOT / "config" / "policies.yaml"
DEFAULT_LOCKS_DIR = DEFAULT_STATE_ROOT / "locks"

# Owner-only modes on platforms that support chmod (Linux).
DIR_MODE_OWNER = 0o700
FILE_MODE_OWNER = 0o600


# --- state-root binding -----------------------------------------------------

@dataclass(frozen=True)
class StatePaths:
    """All mutable operator paths derived from an explicit state_root.

    Artifacts, locks, and logs NEVER come from db.parent — only from state_root.
    """

    state_root: Path
    artifacts_dir: Path
    locks_dir: Path
    logs_dir: Path
    default_db_path: Path


class StateRootError(ValueError):
    """Invalid or unsafe state root / path under state root."""


def _is_symlink(path: Path) -> bool:
    """True if path exists as a symlink (lexists + is_symlink)."""
    try:
        return path.is_symlink()
    except OSError:
        return False


def resolve_state_root(path: Path | str | None = None) -> Path:
    """Resolve state_root; reject if the path itself is a symlink.

    Returns an absolute resolved Path. Does not create the directory.
    """
    raw = Path(path) if path is not None else DEFAULT_STATE_ROOT
    # Reject symlinked state roots before following
    if _is_symlink(raw):
        raise StateRootError(f"symlinked state roots are rejected: {raw}")
    # Also reject if any existing ancestor component is a symlink pointing such
    # that the lexically given path is a symlink at the leaf.
    # Check each path component from root to leaf for symlink-ness of the
    # cumulative path as specified (not fully resolved).
    parts = raw.parts
    if raw.is_absolute():
        cur = Path(parts[0])  # '/' on POSIX
        start = 1
    else:
        cur = Path(parts[0]) if parts else Path(".")
        start = 1
        if _is_symlink(cur):
            raise StateRootError(f"symlinked state roots are rejected: {raw}")
    for part in parts[start:]:
        cur = cur / part
        if _is_symlink(cur):
            raise StateRootError(f"symlinked state roots are rejected: {raw}")
    resolved = raw.resolve()
    return resolved


def is_under_root(path: Path | str, root: Path | str) -> bool:
    """Return True if realpath(path) is root or a descendant of realpath(root)."""
    try:
        real_path = Path(os.path.realpath(str(path)))
        real_root = Path(os.path.realpath(str(root)))
        real_path.relative_to(real_root)
        return True
    except (ValueError, OSError):
        return False


def ensure_within_state_root(path: Path | str, state_root: Path | str) -> Path:
    """Resolve path and require it stays inside resolved state_root.

    Raises StateRootError on escape. Uses realpath to detect symlink escapes.
    """
    root = Path(os.path.realpath(str(state_root)))
    candidate = Path(os.path.realpath(str(path)))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise StateRootError(
            f"path escapes state-root: {path!s} not under {root}"
        ) from exc
    return candidate


def chmod_owner_only(path: Path, *, is_dir: bool) -> None:
    """Best-effort owner-only permissions (0o700 dirs / 0o600 files)."""
    mode = DIR_MODE_OWNER if is_dir else FILE_MODE_OWNER
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def mkdir_owner_only(path: Path, *, state_root: Path | None = None) -> Path:
    """Create directory with 0o700; optionally require under state_root."""
    p = Path(path)
    if state_root is not None:
        # Ensure final resolved path will be under state_root (parent must exist
        # or be creatable under root). Check intended path containment.
        root_res = Path(os.path.realpath(str(state_root)))
        # Build path under root without following leaf symlink
        try:
            # If exists, verify
            if p.exists() or _is_symlink(p):
                ensure_within_state_root(p, root_res)
            else:
                # Check parent chain: resolved parent must be under root once created
                parent = p.parent
                if parent.exists() or _is_symlink(parent):
                    ensure_within_state_root(parent, root_res)
        except StateRootError:
            raise
    p.mkdir(mode=DIR_MODE_OWNER, parents=True, exist_ok=True)
    chmod_owner_only(p, is_dir=True)
    if state_root is not None:
        ensure_within_state_root(p, state_root)
    return p


def write_file_owner_only(
    path: Path,
    data: str | bytes,
    *,
    state_root: Path,
) -> Path:
    """Write a file under state_root with 0o600; reject escapes/symlinked targets."""
    p = Path(path)
    root = Path(os.path.realpath(str(state_root)))
    # Reject if path is a symlink (would write through to outside)
    if _is_symlink(p):
        raise StateRootError(f"refusing to write through symlink: {p}")
    mkdir_owner_only(p.parent, state_root=root)
    # Verify destination stays inside after parent create
    dest_check = Path(os.path.realpath(str(p.parent))) / p.name
    ensure_within_state_root(dest_check, root)
    if isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = data
    fd = os.open(
        str(p),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        FILE_MODE_OWNER,
    )
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    chmod_owner_only(p, is_dir=False)
    ensure_within_state_root(p, root)
    return p


def get_state_paths(state_root: Path | str | None = None) -> StatePaths:
    """Derive artifacts/locks/logs (and optional default db) from state_root."""
    root = resolve_state_root(state_root)
    return StatePaths(
        state_root=root,
        artifacts_dir=root / "artifacts",
        locks_dir=root / "locks",
        logs_dir=root / "logs",
        default_db_path=root / "pipeline.sqlite3",
    )


def ensure_state_dirs(paths: StatePaths) -> StatePaths:
    """Create artifacts/locks/logs under state_root with owner-only perms."""
    mkdir_owner_only(paths.state_root)
    mkdir_owner_only(paths.artifacts_dir, state_root=paths.state_root)
    mkdir_owner_only(paths.locks_dir, state_root=paths.state_root)
    mkdir_owner_only(paths.logs_dir, state_root=paths.state_root)
    return paths


def add_state_root_arg(parser: Any, *, default: Path | None = None) -> None:
    """Add --state-root CLI flag (default: project state/)."""
    parser.add_argument(
        "--state-root",
        type=Path,
        default=default if default is not None else DEFAULT_STATE_ROOT,
        help="Explicit state root for artifacts/locks/logs (default: project state/). "
        "Never inferred from --db alone.",
    )


def iso_now() -> str:
    """Return current UTC timestamp in ISO-8601 form with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_json(payload: dict[str, Any]) -> None:
    """Print a structured JSON object to stdout."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def emit(stage: str, status: str, dry_run: bool, message: str, **extra: Any) -> None:
    """Print a structured JSON status line to stdout."""
    payload: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "dry_run": dry_run,
        "message": message,
        "timestamp": iso_now(),
        "project": "proskills",
    }
    payload.update(extra)
    emit_json(payload)


def exit_ok() -> None:
    sys.exit(0)


def exit_fail(code: int = 1) -> None:
    sys.exit(code)


# --- secret redaction -------------------------------------------------------

_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM blocks
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]+-----[\s\S]*?-----END [A-Z0-9 ]+-----",
            re.MULTILINE,
        ),
        "[REDACTED]",
    ),
    # Bearer tokens
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer [REDACTED]"),
    # AWS access key IDs
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    # GitHub PATs / tokens
    (re.compile(r"\b(?:github_pat_|ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9_]{20,}\b"), "[REDACTED]"),
    # Common API key assignment forms
    (
        re.compile(
            r'(?i)\b(api[_-]?key|apikey|access[_-]?token|secret[_-]?key|auth[_-]?token)'
            r'\s*[=:]\s*["\']?[A-Za-z0-9\-._~+/]{16,}["\']?'
        ),
        r"\1=[REDACTED]",
    ),
    # password= assignments
    (
        re.compile(r'(?i)\b(password|passwd|pwd)\s*[=:]\s*["\']?[^\s"\']{4,}["\']?'),
        r"\1=[REDACTED]",
    ),
    # Long hex secrets (32+ hex chars, likely keys/hashes used as secrets)
    (re.compile(r"\b[0-9a-fA-F]{40,}\b"), "[REDACTED]"),
    # Generic sk-/pk- style keys
    (re.compile(r"\b(?:sk|pk|rk)[_-][A-Za-z0-9]{20,}\b"), "[REDACTED]"),
]


def redact_secrets(text: str) -> str:
    """Redact common secret patterns; replace with [REDACTED]."""
    if not text:
        return text
    out = text
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


# --- minimal YAML loader (mappings, lists, scalars, comments; no anchors) ---

def _parse_scalar(raw: str) -> Any:
    s = raw.strip()
    if not s:
        return ""
    # strip inline comments (not inside quotes)
    if s[0] not in ("'", '"') and " #" in s:
        s = s.split(" #", 1)[0].rstrip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    # int / float
    try:
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
    except ValueError:
        pass
    return s


def load_policies(path: Path | str | None = None) -> dict[str, Any]:
    """Load policies.yaml with a restricted line-based YAML subset parser.

    Supports nested mappings, lists of scalars, scalars, and comments.
    Does not support anchors, aliases, multiline blocks, or flow collections.
    """
    p = Path(path) if path else DEFAULT_POLICIES_PATH
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()

    root: dict[str, Any] = {}
    # stack of (indent, container) where container is dict or list
    stack: list[tuple[int, Any]] = [(-1, root)]

    for lineno, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # expand tabs to spaces for indent calc
        expanded = line.replace("\t", "  ")
        indent = len(expanded) - len(expanded.lstrip(" "))
        content = expanded.strip()

        # pop to parent with smaller indent
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]

        if content.startswith("- "):
            item_raw = content[2:].strip()
            if not isinstance(parent, list):
                raise ValueError(f"YAML list item outside list at line {lineno}")
            # list item that is a mapping: "- key: value" or "- key:"
            if ":" in item_raw and not item_raw.startswith(("'", '"')):
                key, _, rest = item_raw.partition(":")
                key = key.strip()
                rest = rest.strip()
                if rest == "" or rest.startswith("#"):
                    d: dict[str, Any] = {key: {}}
                    parent.append(d)
                    stack.append((indent, d[key]))
                else:
                    parent.append({key: _parse_scalar(rest)})
            else:
                parent.append(_parse_scalar(item_raw))
            continue

        if ":" not in content:
            raise ValueError(f"YAML parse error at line {lineno}: expected key:")

        key, _, rest = content.partition(":")
        key = key.strip()
        rest = rest.strip()

        if not isinstance(parent, dict):
            raise ValueError(f"YAML mapping key inside non-dict at line {lineno}")

        if rest == "" or rest.startswith("#"):
            # Look ahead: next non-empty non-comment line determines list vs map
            child_container: Any = {}
            # peek
            for peek in lines[lineno:]:
                peek_exp = peek.replace("\t", "  ")
                if not peek_exp.strip() or peek_exp.lstrip().startswith("#"):
                    continue
                peek_indent = len(peek_exp) - len(peek_exp.lstrip(" "))
                if peek_indent <= indent:
                    break
                if peek_exp.strip().startswith("- "):
                    child_container = []
                break
            parent[key] = child_container
            stack.append((indent, child_container))
        else:
            parent[key] = _parse_scalar(rest)

    return root


def get_limits(policies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return limits section with safe defaults."""
    defaults = {
        "batch_size": 10,
        "max_retries": 3,
        "max_files_per_candidate": 200,
        "max_total_bytes_per_candidate": 5242880,
        "model_review_threshold": 0.7,
    }
    if not policies:
        return defaults
    limits = policies.get("limits") or {}
    out = dict(defaults)
    out.update({k: v for k, v in limits.items() if v is not None})
    return out


def get_artifact_retention(policies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return artifact retention policy with safe defaults."""
    defaults = {
        "structured_evidence_days": 30,
        "retain_candidate_secrets": False,
        "note": "Artifacts must be redacted; purge structured evidence older than retention window",
    }
    if not policies:
        return defaults
    section = policies.get("artifact_retention") or {}
    out = dict(defaults)
    out.update({k: v for k, v in section.items() if v is not None})
    return out


def get_ai_escalation(policies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return AI escalation policy; deterministic critical findings cannot be overridden."""
    defaults = {
        "escalate_when": [
            "ambiguous_medium_findings",
            "ambiguous_high_findings",
            "quality_judgment",
        ],
        "never_override": ["deterministic_critical_findings"],
        "model_review_threshold": 0.7,
    }
    if not policies:
        return defaults
    section = policies.get("ai_escalation") or {}
    out = dict(defaults)
    out.update({k: v for k, v in section.items() if v is not None})
    return out


# --- locking ----------------------------------------------------------------

def safe_event_id(event_id: str) -> str:
    """Sanitize event_id for use as a lock filename (filesystem-safe)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", event_id.strip())
    return cleaned[:200] or "unknown"


class EventLockHeld(Exception):
    """Raised when another process holds the event lock."""

    def __init__(self, event_id: str, lock_path: Path) -> None:
        self.event_id = event_id
        self.lock_path = lock_path
        super().__init__(f"event lock held for {event_id}: {lock_path}")


@contextmanager
def event_lock(
    event_id: str,
    locks_dir: Path | str | None = None,
    *,
    blocking: bool = False,
) -> Iterator[Path]:
    """Acquire an exclusive file lock for an event_id under locks_dir (from state-root).

    Uses fcntl.flock with non-blocking try by default. Raises EventLockHeld if
    another process holds the lock. Always releases and closes on exit.
    """
    directory = Path(locks_dir) if locks_dir else DEFAULT_LOCKS_DIR
    mkdir_owner_only(directory)
    lock_path = directory / f"{safe_event_id(event_id)}.lock"
    if _is_symlink(lock_path):
        raise StateRootError(f"refusing lock through symlink: {lock_path}")
    # Create/open without following symlinks when possible
    fd = os.open(
        str(lock_path),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        FILE_MODE_OWNER,
    )
    chmod_owner_only(lock_path, is_dir=False)
    fh = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError as exc:
            fh.close()
            raise EventLockHeld(event_id, lock_path) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"{event_id}\n{iso_now()}\n")
        fh.flush()
        yield lock_path
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass


# --- database helpers -------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source TEXT,
    payload_json TEXT,
    received_at TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    name TEXT,
    source_uri TEXT,
    local_path TEXT,
    status TEXT,
    created_at TEXT,
    FOREIGN KEY(event_id) REFERENCES events(event_id)
);

CREATE TABLE IF NOT EXISTS stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    summary TEXT,
    UNIQUE(event_id, stage),
    FOREIGN KEY(event_id) REFERENCES events(event_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    stage TEXT,
    kind TEXT,
    path TEXT,
    sha256 TEXT,
    meta_json TEXT,
    created_at TEXT,
    FOREIGN KEY(event_id) REFERENCES events(event_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT,
    decided_at TEXT,
    note TEXT,
    UNIQUE(event_id, action),
    FOREIGN KEY(event_id) REFERENCES events(event_id)
);

CREATE TABLE IF NOT EXISTS outbound_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    outbound_key TEXT NOT NULL UNIQUE,
    stage TEXT,
    status TEXT,
    body_json TEXT,
    created_at TEXT,
    delivered_at TEXT,
    attempt_count INTEGER DEFAULT 0,
    next_attempt_at TEXT,
    FOREIGN KEY(event_id) REFERENCES events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_candidates_event_id ON candidates(event_id);
CREATE INDEX IF NOT EXISTS idx_stage_runs_event_id ON stage_runs(event_id);
CREATE INDEX IF NOT EXISTS idx_stage_runs_stage ON stage_runs(stage);
CREATE INDEX IF NOT EXISTS idx_artifacts_event_id ON artifacts(event_id);
CREATE INDEX IF NOT EXISTS idx_approvals_event_id ON approvals(event_id);
CREATE INDEX IF NOT EXISTS idx_outbound_event_id ON outbound_events(event_id);
CREATE INDEX IF NOT EXISTS idx_outbound_key ON outbound_events(outbound_key);
CREATE INDEX IF NOT EXISTS idx_outbound_status ON outbound_events(status);
"""


def connect_db(path: Path | str) -> sqlite3.Connection:
    """Open SQLite connection with foreign keys enabled."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def begin_immediate(conn: sqlite3.Connection) -> None:
    """Begin a write transaction that fails fast on lock contention."""
    conn.execute("BEGIN IMMEDIATE")


@contextmanager
def with_db(path: Path | str, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Context manager that commits on success and closes the connection."""
    conn = connect_db(path)
    try:
        if immediate:
            begin_immediate(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if missing; migrate outbound_events columns."""
    conn.executescript(SCHEMA_SQL)
    migrate_outbound_events(conn)


def upsert_stage_run(
    conn: sqlite3.Connection,
    event_id: str,
    stage: str,
    status: str,
    summary: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    """Insert or update a stage_run without duplicating (UNIQUE event_id, stage)."""
    now = iso_now()
    started = started_at or now
    finished = finished_at or now
    conn.execute(
        """
        INSERT INTO stage_runs (event_id, stage, status, started_at, finished_at, summary)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id, stage) DO UPDATE SET
            status = excluded.status,
            finished_at = excluded.finished_at,
            summary = excluded.summary
        """,
        (event_id, stage, status, started, finished, summary),
    )


def get_stage_run(
    conn: sqlite3.Connection, event_id: str, stage: str
) -> sqlite3.Row | None:
    """Return existing stage_run row or None."""
    return conn.execute(
        "SELECT * FROM stage_runs WHERE event_id = ? AND stage = ?",
        (event_id, stage),
    ).fetchone()


# --- path safety ------------------------------------------------------------

def resolve_under_root(root: Path | str, user_path: Path | str) -> Path:
    """Resolve user_path under root; reject traversal and absolute escapes.

    Raises ValueError if the path contains '..' or the resolved path escapes root.
    Absolute paths are allowed only when they resolve inside root.
    """
    root_resolved = Path(root).resolve()
    raw_str = str(user_path)
    # Reject obvious traversal tokens before resolve
    if ".." in Path(raw_str).parts:
        raise ValueError(f"path traversal rejected: '..' not allowed in {user_path!s}")

    raw = Path(user_path)
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        candidate = (root_resolved / raw).resolve()

    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(
            f"path traversal rejected: {user_path!s} escapes root {root_resolved}"
        ) from exc

    return candidate


def resolve_candidate_path(user_path: str | Path, project_root: Path | None = None) -> Path:
    """Resolve a candidate local path under PROJECT_ROOT (or given root)."""
    root = project_root or PROJECT_ROOT
    return resolve_under_root(root, user_path)


def path_escapes_root(path: Path, root: Path) -> bool:
    """Return True if resolved path is outside root (symlink-aware)."""
    try:
        path.resolve().relative_to(root.resolve())
        return False
    except ValueError:
        return True


def iter_files_no_follow(root: Path) -> Iterator[Path]:
    """Yield regular files under root without following directory symlinks.

    Symlink files are yielded as themselves (caller must not delete realpath
    targets outside root). Symlink directories are skipped (not descended).
    """
    root_path = Path(root)
    if not root_path.is_dir() or _is_symlink(root_path):
        return

    stack = [root_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        is_link = entry.is_symlink()
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError:
                        continue
                    p = Path(entry.path)
                    if is_link:
                        # Yield symlink paths so callers may remove the link
                        # inode itself, but never descend through link dirs.
                        if not is_dir:
                            yield p
                        continue
                    if is_dir:
                        stack.append(p)
                    elif is_file:
                        yield p
        except OSError:
            continue


def safe_unlink_under_root(path: Path, root: Path) -> bool:
    """Unlink path only if it cannot delete content outside root.

    - Symlinks: unlink the link inode only (does not remove the target).
    - Regular files: require realpath under root, then unlink.
    Returns True if unlinked.
    """
    p = Path(path)
    root_real = Path(os.path.realpath(str(root)))
    try:
        if p.is_symlink():
            # Removing the symlink does not delete the outside target.
            # Still require the symlink path itself to live under root.
            link_parent = Path(os.path.realpath(str(p.parent)))
            try:
                link_parent.relative_to(root_real)
            except ValueError:
                return False
            p.unlink(missing_ok=True)
            return True
        real = Path(os.path.realpath(str(p)))
        try:
            real.relative_to(root_real)
        except ValueError:
            return False
        if real.is_file():
            real.unlink(missing_ok=True)
            return True
    except OSError:
        return False
    return False


# --- dry-run / apply CLI helpers --------------------------------------------

def add_dry_run_apply_flags(parser: Any) -> None:
    """Add --apply and compatibility --dry-run. Default is dry-run (no writes)."""
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Persist writes. Without this flag, dry-run=True (no writes).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Explicit dry-run (default without --apply). Kept for compatibility.",
    )


def resolve_dry_run(args: Any) -> bool:
    """Return True unless --apply was passed. --apply always wins over --dry-run."""
    if getattr(args, "apply", False):
        return False
    return True


def contains_secret_like(text: str) -> bool:
    """True if redact_secrets would alter text (secret-like content detected)."""
    if text is None:
        return False
    return redact_secrets(text) != text


# --- insecure-for-tests gating ----------------------------------------------

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_host(host: str | None) -> bool:
    """True if host is a loopback name/address."""
    if not host:
        return False
    h = host.lower().strip("[]")
    return h in LOOPBACK_HOSTS


def assert_insecure_for_tests_allowed(endpoint_url: str) -> None:
    """Hard-error unless PROSKILLS_ENV=test AND endpoint host is loopback.

    Call when --insecure-for-tests is requested. Impossible outside test env.
    """
    if os.environ.get("PROSKILLS_ENV") != "test":
        raise RuntimeError(
            "--insecure-for-tests is only allowed when PROSKILLS_ENV=test"
        )
    # Lazy import avoidance: parse URL here
    from urllib.parse import urlparse

    host = urlparse(endpoint_url).hostname
    if not is_loopback_host(host):
        raise RuntimeError(
            f"--insecure-for-tests rejects non-loopback host: {host!r}"
        )


# --- oracle delivery policy -------------------------------------------------

def get_oracle_delivery(policies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return oracle.delivery policy with safe defaults (loopback allowlist for tests)."""
    defaults = {
        "project": "proskills-md",
        "allowed_hostnames": ["localhost", "127.0.0.1"],
        "connect_timeout_sec": 3,
        "read_timeout_sec": 5,
        "max_retries": 5,
        "backoff_base_sec": 1,
        "backoff_max_sec": 60,
        "require_https": True,
        "allow_redirects": False,
    }
    if not policies:
        return defaults
    oracle = policies.get("oracle") or {}
    delivery = oracle.get("delivery") or {}
    out = dict(defaults)
    if oracle.get("project"):
        out["project"] = oracle["project"]
    for k, v in delivery.items():
        if v is not None:
            out[k] = v
    return out


def migrate_outbound_events(conn: sqlite3.Connection) -> None:
    """Ensure outbound_events has attempt_count / next_attempt_at columns."""
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(outbound_events)").fetchall()
    }
    if not cols:
        return  # table not created yet; SCHEMA_SQL handles fresh create
    if "attempt_count" not in cols:
        conn.execute(
            "ALTER TABLE outbound_events ADD COLUMN attempt_count INTEGER DEFAULT 0"
        )
    if "next_attempt_at" not in cols:
        conn.execute(
            "ALTER TABLE outbound_events ADD COLUMN next_attempt_at TEXT"
        )


