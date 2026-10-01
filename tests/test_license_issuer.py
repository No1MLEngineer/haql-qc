"""Tests for the issuer-side license tool.

These live outside the package on purpose. ``tools/haql_license.py`` holds the
signing side and must never ship in the wheel, so the test has to reach it by
path rather than by import.

The signer is pure Python to keep the issuer installable without a crypto
package. That choice is only safe if the result is provably identical to a
reference implementation, which is what the differential test below is for.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ISSUER = REPO / "tools" / "haql_license.py"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _load_issuer_module():
    spec = importlib.util.spec_from_file_location("_issuer_under_test", ISSUER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


issuer = _load_issuer_module()


class TestSigner(unittest.TestCase):
    """The signer must be indistinguishable from a reference ed25519."""

    def test_matches_a_reference_implementation(self):
        """Differential test against ``cryptography``, when it is available.

        Fixed seeds and fixed messages, chosen to cover the awkward cases: the
        empty message, single bytes, a two-byte message, and a realistic JSON
        payload. Random seeds would also work but a failure would not be
        reproducible from the test output alone.
        """
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
        except ImportError:
            self.skipTest("cryptography not installed")

        seeds = [
            bytes(range(32)),
            bytes.fromhex(
                "d75a980182b10ab7d54bfed3c964073a"
                "0ee172f3daa62325af021a68f707511a"
            ),
            bytes.fromhex(
                "4ccd089b28ff96da9db6c346ec114e0f"
                "5b8a319f35aba624da8cf6ed4fb8a6fb"
            ),
        ]
        messages = [
            b"",
            b"\x72",
            b"\xaf\x82",
            b"a message that is longer than one hash block",
            json.dumps(
                {"comp": "Test", "tier": "team", "seats": 10},
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
        ]
        for seed in seeds:
            reference = Ed25519PrivateKey.from_private_bytes(seed)
            for message in messages:
                with self.subTest(seed=seed[:4].hex(), msg=message[:8]):
                    self.assertEqual(
                        issuer.sign(seed, message), reference.sign(message)
                    )

    def test_signing_is_deterministic(self):
        """Ed25519 is deterministic, so a re-sign must reproduce the bytes."""
        seed = bytes(range(32))
        self.assertEqual(issuer.sign(seed, b"x"), issuer.sign(seed, b"x"))

    def test_a_different_message_gives_a_different_signature(self):
        """Guards against a sign() that ignores its message argument."""
        seed = bytes(range(32))
        self.assertNotEqual(issuer.sign(seed, b"a"), issuer.sign(seed, b"b"))


class TestPayload(unittest.TestCase):
    def test_payload_carries_every_field_the_verifier_requires(self):
        payload = issuer.decode_payload_bytes(
            issuer.build_payload("Acme Oil", "team", 7, date(2030, 1, 1))
        )
        self.assertEqual(payload["comp"], "Acme Oil")
        self.assertEqual(payload["tier"], "team")
        self.assertEqual(payload["seats"], 7)
        self.assertEqual(payload["exp"], "2030-01-01")
        self.assertEqual(payload["iss"], issuer.ISSUER)
        self.assertEqual(len(payload["nonce"]), 32)

    def test_every_token_gets_a_distinct_nonce(self):
        """Two customers on identical terms must not receive the same token."""
        exp = date(2030, 1, 1)
        nonces = {
            issuer.decode_payload_bytes(
                issuer.build_payload("Acme Oil", "team", 7, exp)
            )["nonce"]
            for _ in range(25)
        }
        self.assertEqual(len(nonces), 25)

    def test_field_names_are_short_and_stable(self):
        """The verifier reads these exact keys, so they are a wire format."""
        payload = issuer.decode_payload_bytes(
            issuer.build_payload("Acme", "desktop", 1, date(2030, 1, 1))
        )
        self.assertEqual(
            set(payload),
            {"comp", "tier", "exp", "seats", "iss", "nonce"},
        )


class TestRegistry(unittest.TestCase):
    """The registry is a customer record, not just a blacklist."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "registry.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _token(self, company="Acme Oil", nonce=None):
        payload = json.dumps(
            {
                "comp": company,
                "tier": "team",
                "exp": "2030-01-01",
                "seats": 3,
                "iss": "haql",
                "nonce": nonce or "a" * 32,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return f"HAQL1.{_b64(payload)}.{_b64(b'0' * 64)}"

    def test_issuing_records_under_issued_not_reinstated(self):
        """Regression: issuance used to land in the `reinstated` list.

        That made the registry unable to answer the only question it is really
        asked -- who currently holds a license.
        """
        issuer.record_event(self._token(), self.path, "issued")
        reg = json.loads(self.path.read_text())
        self.assertEqual(len(reg.get("issued", [])), 1)
        self.assertEqual(reg.get("revoked", []), [])
        self.assertEqual(reg.get("reinstated", []), [])

    def test_the_three_states_stay_separate(self):
        for action, bucket in (
            ("issued", "issued"),
            ("revoke", "revoked"),
            ("reinstate", "reinstated"),
        ):
            issuer.record_event(
                self._token(nonce=action.ljust(32, "0")), self.path, action
            )
        reg = json.loads(self.path.read_text())
        self.assertEqual(len(reg["issued"]), 1)
        self.assertEqual(len(reg["revoked"]), 1)
        self.assertEqual(len(reg["reinstated"]), 1)

    def test_recording_twice_does_not_duplicate(self):
        token = self._token()
        issuer.record_event(token, self.path, "issued")
        issuer.record_event(token, self.path, "issued")
        reg = json.loads(self.path.read_text())
        self.assertEqual(len(reg["issued"]), 1)

    def test_a_registry_that_is_not_json_is_never_overwritten(self):
        """Clobbering a registry would destroy the revocation history."""
        self.path.write_text("{ this is not json")
        with self.assertRaises(SystemExit):
            issuer.record_event(self._token(), self.path, "issued")
        self.assertEqual(self.path.read_text(), "{ this is not json")

    def test_a_token_with_no_nonce_is_refused(self):
        import base64

        payload = json.dumps({"comp": "X", "tier": "team"}).encode()
        b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        with self.assertRaises(SystemExit):
            issuer.record_event(f"HAQL1.{b64}.x", self.path, "issued")


class TestExpiryParsing(unittest.TestCase):
    def test_plus_n_means_n_days_from_today(self):
        self.assertEqual(
            issuer._parse_expiry("+30"), date.today() + timedelta(days=30)
        )

    def test_absolute_iso_date_is_taken_literally(self):
        self.assertEqual(issuer._parse_expiry("2029-12-31"), date(2029, 12, 31))

    def test_garbage_is_refused(self):
        for bad in ("tomorrow", "+", "+4.5", "31/12/2029"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                issuer._parse_expiry(bad)


class TestTheIssuerDoesNotShip(unittest.TestCase):
    """The whole security model rests on the private half staying outside."""

    def test_the_package_never_contains_a_signing_key(self):
        from haql_qc import licensing

        pkg_dir = Path(licensing.__file__).parent
        offenders = [
            p
            for p in pkg_dir.rglob("*.py")
            if "def sign" in p.read_text(encoding="utf-8")
            or "private" in p.name.lower()
        ]
        self.assertEqual(offenders, [], f"signing code inside the package: {offenders}")

    def test_the_issuer_is_outside_the_package_directory(self):
        from haql_qc import licensing

        self.assertNotEqual(ISSUER.parent, Path(licensing.__file__).parent)

    def test_no_32_byte_blob_is_committed_as_a_key(self):
        """A raw 32-byte file in the tree is almost certainly a private seed."""
        suspicious = [
            p
            for p in REPO.rglob("*")
            if p.is_file()
            and p.suffix in ("", ".key", ".seed")
            and ".venv" not in p.parts
            and p.stat().st_size == 32
        ]
        self.assertEqual(suspicious, [])


class TestIssuerCli(unittest.TestCase):
    def test_verify_reports_a_forged_token_as_invalid(self):
        """End to end through the process, not just the imported functions."""
        tampered = "HAQL1." + "eyJjb21wIjoiQSJ9" + ".AAAA"
        proc = subprocess.run(
            [sys.executable, str(ISSUER), "--verify", tampered],
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"},
        )
        out = json.loads(proc.stdout)
        self.assertFalse(out["valid"])
        self.assertIsNotNone(out["reason"])


if __name__ == "__main__":
    unittest.main()