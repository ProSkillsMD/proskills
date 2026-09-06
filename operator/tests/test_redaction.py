"""redact_secrets masks common tokens."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from _common import redact_secrets  # noqa: E402


class TestRedaction(unittest.TestCase):
    def test_bearer(self) -> None:
        text = "Authorization: Bearer abcdefghijklmnopQRSTUV123456"
        out = redact_secrets(text)
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("abcdefghijklmnopQRSTUV123456", out)

    def test_github_pat(self) -> None:
        text = "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        out = redact_secrets(text)
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123456789", out)

    def test_aws_key(self) -> None:
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        out = redact_secrets(text)
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)

    def test_password_assignment(self) -> None:
        text = "password=supersecretvalue"
        out = redact_secrets(text)
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("supersecretvalue", out)

    def test_pem_block(self) -> None:
        text = "-----BEGIN PRIVATE KEY-----\nABC123XYZ\n-----END PRIVATE KEY-----"
        out = redact_secrets(text)
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("ABC123XYZ", out)

    def test_long_hex(self) -> None:
        hexsecret = "a" * 40
        out = redact_secrets(f"digest={hexsecret}")
        self.assertIn("[REDACTED]", out)
        self.assertNotIn(hexsecret, out)

    def test_sk_style_key(self) -> None:
        text = "api_key=sk_test_abcdefghijklmnopqrstuvwxyz1234"
        out = redact_secrets(text)
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("sk_test_abcdefghijklmnopqrstuvwxyz1234", out)

    def test_flagged_fixture_secret_redacted_in_scan_snippet(self) -> None:
        from static_scan import scan_candidate

        flagged = Path(__file__).resolve().parents[1] / "fixtures" / "flagged-skill"
        result = scan_candidate(
            flagged,
            {"max_files_per_candidate": 200, "max_total_bytes_per_candidate": 5242880},
        )
        blob = str(result)
        self.assertNotIn("sk_test_abcdefghijklmnopqrstuvwxyz1234", blob)


if __name__ == "__main__":
    unittest.main()
