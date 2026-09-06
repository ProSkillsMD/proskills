#!/usr/bin/env python3
"""Local mock Oracle HTTPS server for tests (loopback only).

Uses ssl + http.server with an ephemeral self-signed cert (openssl CLI for
cert generation; Python stdlib for the HTTPS server). Counts POST bodies by
outbound_key and returns HMAC-signed acks. Never used against non-loopback hosts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


def generate_self_signed_cert(directory: Path) -> tuple[Path, Path]:
    """Create ephemeral self-signed cert+key under directory via openssl."""
    cert = directory / "mock_oracle.crt"
    key = directory / "mock_oracle.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    # Restrict key perms
    os.chmod(key, 0o600)
    return cert, key


def compute_ack_sig(outbound_key: str, ack_secret: str) -> str:
    return hmac.new(
        ack_secret.encode("utf-8"),
        outbound_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class MockOracleState:
    """Shared mutable state for the mock server."""

    def __init__(self, ack_secret: str) -> None:
        self.ack_secret = ack_secret
        self.lock = threading.Lock()
        self.post_counts: dict[str, int] = {}
        self.post_bodies: dict[str, list[Any]] = {}
        self.total_posts = 0
        self.require_auth = True
        self.expected_bearer: str | None = None

    def record(self, outbound_key: str, body: Any) -> None:
        with self.lock:
            self.total_posts += 1
            self.post_counts[outbound_key] = self.post_counts.get(outbound_key, 0) + 1
            self.post_bodies.setdefault(outbound_key, []).append(body)

    def count_for(self, outbound_key: str) -> int:
        with self.lock:
            return int(self.post_counts.get(outbound_key, 0))


def make_handler(state: MockOracleState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            # Quiet — avoid leaking anything to stderr in tests
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(max(0, min(length, 1_000_000)))
            auth = self.headers.get("Authorization", "")
            if state.require_auth and state.expected_bearer:
                expected = f"Bearer {state.expected_bearer}"
                if auth != expected:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return

            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"bad_json"}')
                return

            outbound_key = (
                body.get("outbound_key")
                or self.headers.get("X-Outbound-Key")
                or ""
            )
            if not outbound_key:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"missing_outbound_key"}')
                return

            state.record(str(outbound_key), body)
            ack = {
                "outbound_key": outbound_key,
                "status": "accepted",
                "sig": compute_ack_sig(str(outbound_key), state.ack_secret),
            }
            payload = json.dumps(ack).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(405)
            self.end_headers()

    return Handler


class MockOracleHTTPSServer:
    """Context-manager mock Oracle HTTPS on 127.0.0.1."""

    def __init__(
        self,
        ack_secret: str,
        *,
        bearer_token: str = "test-bearer-token",
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.ack_secret = ack_secret
        self.bearer_token = bearer_token
        self.host = host
        self.port_requested = port
        self.state = MockOracleState(ack_secret)
        self.state.expected_bearer = bearer_token
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self.cert_path: Path | None = None
        self.key_path: Path | None = None
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self.httpd is not None
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self.port}/oracle/events"

    def __enter__(self) -> "MockOracleHTTPSServer":
        if self.host not in ("127.0.0.1", "localhost"):
            raise RuntimeError("mock Oracle may only bind loopback")
        self._tmpdir = tempfile.TemporaryDirectory(prefix="mock_oracle_")
        tdir = Path(self._tmpdir.name)
        self.cert_path, self.key_path = generate_self_signed_cert(tdir)
        handler = make_handler(self.state)
        self.httpd = ThreadingHTTPServer((self.host, self.port_requested), handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.cert_path), str(self.key_path))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
        self.httpd = None
        self._tmpdir = None

    def count_for(self, outbound_key: str) -> int:
        return self.state.count_for(outbound_key)


# Also expose as scripts/testing/mock_oracle_server.py convenience
def main() -> None:
    """Manual smoke: run briefly on a free port (not for production)."""
    import time

    secret = os.environ.get("ORACLE_ACK_SECRET", "dev-ack-secret")
    token = os.environ.get("ORACLE_BEARER_TOKEN", "dev-bearer")
    with MockOracleHTTPSServer(secret, bearer_token=token) as srv:
        print(json.dumps({"url": srv.url, "cert": str(srv.cert_path)}))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
