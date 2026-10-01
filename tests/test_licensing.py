"""License parsing, signature verification, and enforcement.

The point of these tests is that a tampered license must fail. Everything else
is in service of that: an expiry in the past, an unknown tier, a truncated
token, a flipped byte in the signature. A verifier that only passes on good
input is worthless, so each rejection path is pinned here.

Signatures are produced with the same RFC 8032 arithmetic the module verifies
with. That is deliberate self-consistency, not a proof of interoperability --
the RFC 8032 test vectors below are the external check.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from haql_qc.licensing import (
    DEFAULT_LICENSE_PATHS,
    EMBEDDED_PUBLIC_KEY,
    TIERS,
    License,
    LicenseError,
    check,
    check_revocations,
    ed25519_verify,
    parse_token,
    verify_token,
)


# --------------------------------------------------------------------------
# A minimal signer, so tests can mint tokens without a private key dependency.
# This mirrors haql-license-win.exe: HAQL1.<b64url(json)>.<b64url(sig)>
# --------------------------------------------------------------------------

import hashlib

from haql_qc import licensing as _L


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _encode_point(p) -> bytes:
    zi = pow(p[2], _L._Q - 2, _L._Q)
    x = p[0] * zi % _L._Q
    y = p[1] * zi % _L._Q
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _clamp(h: bytes) -> int:
    a = bytearray(h[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(bytes(a), "little")


def public_key(seed: bytes) -> bytes:
    return _encode_point(_L._pt_mul(_L._B, _clamp(hashlib.sha512(seed).digest())))


def sign(seed: bytes, message: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = _clamp(h)
    prefix = h[32:]
    pub = public_key(seed)
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L._L
    rs = _encode_point(_L._pt_mul(_L._B, r))
    k = int.from_bytes(hashlib.sha512(rs + pub + message).digest(), "little") % _L._L
    s = (r + k * a) % _L._L
    return rs + s.to_bytes(32, "little")


SEED = bytes(range(32))
TEST_PUB = public_key(SEED)


def mint(payload: dict, seed: bytes = SEED, *, pad_to: int | None = None) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    if pad_to:
        raw = raw + b"\x00" * (pad_to - len(raw))
    sig = sign(seed, raw.split(b"\x00", 1)[0])
    return f"HAQL1.{_b64url(raw)}.{_b64url(sig)}"


def good_payload(**over) -> dict:
    p = {
        "comp": "Test Operator Ltd",
        "tier": "team",
        "seats": 5,
        "exp": "2099-12-31",
        "iss": "haql",
        "nonce": "test-nonce-0001",
    }
    p.update(over)
    return p


class TestEd25519(unittest.TestCase):
    """RFC 8032 section 7.1 vectors. These are the external correctness check."""

    VECTORS = [
        (
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
            "",
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
            "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
        ),
        (
            "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
            "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
            "72",
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da08"
            "5ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
        ),
        (
            "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
            "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
            "af82",
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18"
            "ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
        ),
    ]

    def test_rfc8032_vectors_verify(self):
        for sk, pk, msg, sig in self.VECTORS:
            with self.subTest(pk=pk[:16]):
                self.assertTrue(
                    ed25519_verify(
                        bytes.fromhex(pk), bytes.fromhex(msg), bytes.fromhex(sig)
                    ),
                    "must verify the published RFC 8032 vector",
                )

    def test_rfc8032_vectors_are_not_wrongly_accepted(self):
        sk, pk, msg, sig = self.VECTORS[0]
        key, message, signature = (
            bytes.fromhex(pk),
            bytes.fromhex(msg),
            bytes.fromhex(sig),
        )
        self.assertFalse(ed25519_verify(key, message + b"x", signature))
        flipped = bytearray(signature)
        flipped[0] ^= 0x01
        self.assertFalse(ed25519_verify(key, message, bytes(flipped)))
        self.assertFalse(ed25519_verify(bytes(32), message, signature))
        self.assertFalse(ed25519_verify(key, message, signature[:-1]))
        self.assertFalse(ed25519_verify(key, message, bytes(64)))

    def test_derived_key_is_stable(self):
        """The test signer must agree with itself, or the fixtures are noise."""
        self.assertEqual(public_key(SEED), public_key(SEED))
        self.assertNotEqual(public_key(SEED), public_key(bytes(32)))


class TestValidLicense(unittest.TestCase):
    def test_a_well_formed_signed_license_verifies(self):
        r = verify_token(mint(good_payload()), public_key=TEST_PUB)
        self.assertTrue(r.valid, r.reason)
        self.assertEqual(r.license.company, "Test Operator Ltd")
        self.assertEqual(r.license.tier, "team")
        self.assertEqual(r.license.seats, 5)
        self.assertEqual(r.license.nonce, "test-nonce-0001")

    def test_seats_of_zero_means_unlimited(self):
        r = verify_token(mint(good_payload(seats=0)), public_key=TEST_PUB)
        self.assertTrue(r.valid, r.reason)
        self.assertTrue(r.license.unlimited_seats)

    def test_padded_payload_is_accepted(self):
        """The issuer writes into a fixed buffer, so NUL padding is normal.

        The real Volve-era registry contains a token padded to 235 bytes.
        """
        token = mint(good_payload(), pad_to=256)
        payload, msg, _ = parse_token(token)
        self.assertLess(
            len(msg), 256, "padding must be stripped from the signed message"
        )
        r = verify_token(token, public_key=TEST_PUB)
        self.assertTrue(r.valid, r.reason)
        self.assertEqual(r.license.company, "Test Operator Ltd")

    def test_days_remaining_and_expiry(self):
        r = verify_token(mint(good_payload(exp="2099-01-01")), public_key=TEST_PUB)
        self.assertTrue(r.valid)
        days = r.license.days_remaining(today=date(2098, 12, 31))
        self.assertEqual(days, 1)
        self.assertFalse(r.license.is_expired(today=date(2098, 12, 31)))
        self.assertTrue(r.license.is_expired(today=date(2099, 1, 2)))

    def test_summary_is_json_serialisable(self):
        r = verify_token(mint(good_payload()), public_key=TEST_PUB)
        blob = json.dumps(r.summary())
        self.assertIn("Test Operator Ltd", blob)
        self.assertIn("valid", blob)


class TestTampering(unittest.TestCase):
    """The core property. Each of these edits the customer could make."""

    def test_extending_the_expiry_breaks_the_signature(self):
        token = mint(good_payload(exp="2027-06-30"))
        payload, _, sig = parse_token(token)
        payload["exp"] = "2099-12-31"
        forged = f"HAQL1.{_b64url(json.dumps(payload, separators=(',', ':')).encode())}.{_b64url(sig)}"
        r = verify_token(forged, public_key=TEST_PUB)
        self.assertNotEqual(forged, token, "the forgery must actually differ")
        self.assertFalse(r.valid, "extending the expiry must invalidate the signature")
        self.assertIn("signature", r.reason)

    def test_upgrading_the_tier_breaks_the_signature(self):
        token = mint(good_payload(tier="desktop"))
        payload, _, sig = parse_token(token)
        payload["tier"] = "enterprise"
        payload["seats"] = 999
        forged = f"HAQL1.{_b64url(json.dumps(payload, separators=(',', ':')).encode())}.{_b64url(sig)}"
        r = verify_token(forged, public_key=TEST_PUB)
        self.assertFalse(r.valid, "a customer must not be able to self-upgrade")

    def test_a_different_key_cannot_mint_a_valid_license(self):
        token = mint(good_payload(), seed=bytes(reversed(SEED)))
        r = verify_token(token, public_key=TEST_PUB)
        self.assertFalse(r.valid)
        self.assertIn("signature", r.reason)

    def test_truncated_signature_is_rejected(self):
        token = mint(good_payload())
        prefix, payload, sig = token.split(".")
        r = verify_token(f"{prefix}.{payload}.{sig[:40]}", public_key=TEST_PUB)
        self.assertFalse(r.valid)

    def test_swapped_payload_is_rejected(self):
        a = mint(good_payload(comp="Alpha")).split(".")
        b = mint(good_payload(comp="Bravo")).split(".")
        spliced = f"{a[0]}.{b[1]}.{a[2]}"
        r = verify_token(spliced, public_key=TEST_PUB)
        self.assertFalse(r.valid)


class TestMalformed(unittest.TestCase):
    def test_empty_string(self):
        r = verify_token("", public_key=TEST_PUB)
        self.assertFalse(r.valid)

    def test_wrong_prefix(self):
        r = verify_token("XYZ1.abc.def", public_key=TEST_PUB)
        self.assertFalse(r.valid)
        self.assertIn("expected HAQL1", r.reason)

    def test_too_few_segments(self):
        r = verify_token("HAQL1.onlypayload", public_key=TEST_PUB)
        self.assertFalse(r.valid)

    def test_non_base64_segments(self):
        r = verify_token("HAQL1.!!!not-base64!!!.@@@", public_key=TEST_PUB)
        self.assertFalse(r.valid)

    def test_payload_is_not_json(self):
        r = verify_token(f"HAQL1.{_b64url(b'not json')}.{_b64url(bytes(64))}", public_key=TEST_PUB)
        self.assertFalse(r.valid)
        self.assertIn("JSON", r.reason)

    def test_payload_is_a_json_array_not_an_object(self):
        r = verify_token(
            f"HAQL1.{_b64url(b'[1,2,3]')}.{_b64url(bytes(64))}", public_key=TEST_PUB
        )
        self.assertFalse(r.valid)

    def test_missing_required_field(self):
        payload = good_payload()
        del payload["comp"]
        r = verify_token(mint(payload), public_key=TEST_PUB)
        self.assertFalse(r.valid)
        self.assertIn("comp", r.reason)

    def test_unparseable_expiry(self):
        r = verify_token(mint(good_payload(exp="31/12/2099")), public_key=TEST_PUB)
        self.assertFalse(r.valid)
        self.assertIn("expiry", r.reason.lower())

    def test_non_integer_seats(self):
        r = verify_token(mint(good_payload(seats="five")), public_key=TEST_PUB)
        self.assertFalse(r.valid)

    def test_unknown_tier_is_rejected_even_when_signed(self):
        """A signed license with a tier this build does not know is refused.

        Silently accepting it would mean a customer pays for a tier whose
        entitlements nobody has implemented.
        """
        r = verify_token(mint(good_payload(tier="platinum")), public_key=TEST_PUB)
        self.assertFalse(r.valid)
        self.assertIn("unknown tier", r.reason)


class TestExpiry(unittest.TestCase):
    def test_expired_license_is_invalid_but_still_reported(self):
        r = verify_token(
            mint(good_payload(exp="2020-01-01")),
            public_key=TEST_PUB,
            today=date(2026, 1, 1),
        )
        self.assertFalse(r.valid)
        self.assertIn("expired", r.reason)
        self.assertIsNotNone(r.license, "an expired license should still be readable")
        self.assertEqual(r.license.company, "Test Operator Ltd")

    def test_expiry_is_checked_against_the_supplied_date(self):
        token = mint(good_payload(exp="2030-06-01"))
        self.assertTrue(verify_token(token, public_key=TEST_PUB, today=date(2029, 1, 1)).valid)
        self.assertFalse(verify_token(token, public_key=TEST_PUB, today=date(2031, 1, 1)).valid)

    def test_a_license_expiring_today_is_still_valid(self):
        r = verify_token(
            mint(good_payload(exp="2030-06-01")), public_key=TEST_PUB, today=date(2030, 6, 1)
        )
        self.assertTrue(r.valid, "expiry is end-of-day, not start-of-day")


class TestDiscovery(unittest.TestCase):
    """Where the license comes from matters, and is recorded."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {k: os.environ.get(k) for k in ("HAQL_LICENSE", "HAQL_LICENSE_FILE")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_explicit_path_wins(self):
        p = self.tmp / "lic.key"
        p.write_text(mint(good_payload()), encoding="utf-8")
        r = check(p, public_key=TEST_PUB)
        self.assertTrue(r.valid, r.reason)
        self.assertEqual(r.source, str(p))

    def test_missing_explicit_path_is_a_reason_not_a_crash(self):
        r = check(self.tmp / "nope.key", public_key=TEST_PUB)
        self.assertFalse(r.valid)
        self.assertIn("not found", r.reason)

    def test_env_var_carries_the_token_itself(self):
        os.environ["HAQL_LICENSE"] = mint(good_payload())
        r = check(public_key=TEST_PUB)
        self.assertTrue(r.valid, r.reason)
        self.assertIn("HAQL_LICENSE", r.source)

    def test_env_var_file_path(self):
        p = self.tmp / "lic.key"
        p.write_text(mint(good_payload()), encoding="utf-8")
        os.environ["HAQL_LICENSE_FILE"] = str(p)
        r = check(public_key=TEST_PUB)
        self.assertTrue(r.valid, r.reason)
        self.assertEqual(r.source, str(p))

    def test_env_var_file_path_missing_file_is_reported(self):
        os.environ["HAQL_LICENSE_FILE"] = str(self.tmp / "absent")
        r = check(public_key=TEST_PUB)
        self.assertFalse(r.valid)
        self.assertIn("missing file", r.reason)

    def test_no_license_anywhere_is_a_result_not_a_crash(self):
        os.environ.pop("HAQL_LICENSE", None)
        os.environ.pop("HAQL_LICENSE_FILE", None)
        r = check(public_key=TEST_PUB)
        self.assertIsInstance(r.valid, bool)
        if not r.valid:
            # Either nothing was found, or something was found and rejected.
            # Both are legitimate outcomes; an exception would not be.
            self.assertTrue(r.reason, "a failed check must explain itself")

    def test_a_non_haql_file_at_the_default_path_is_reported_clearly(self):
        """A wrong-format key sitting at a search path must name itself.

        Finding it and saying so is correct behaviour. Silently ignoring it, or
        crashing, would not be. This matters in practice: the build machine has
        a ``v=1`` semicolon-format key at ``~/.haql/license.key`` belonging to a
        different tool, which is exactly why haql-qc does not search there.

        The path is passed explicitly rather than relied on via the search list,
        so the assertion tests the reporting and not this machine's home dir.
        """
        p = Path.home() / ".haql" / "license.key"
        if not p.exists() or p.read_text(encoding="utf-8").startswith("HAQL1."):
            self.skipTest("no legacy-format key available to test against")
        r = check(p, public_key=TEST_PUB)
        self.assertFalse(r.valid)
        self.assertIn("HAQL1", r.reason)
        self.assertIn(str(p), r.reason)

    def test_the_default_search_path_does_not_claim_a_shared_dot_directory(self):
        """``~/.haql`` belongs to another tool on this machine.

        Searching it would mean reading another program's private files and then
        reporting its contents as a licensing failure, so the dot-directory is
        deliberately absent from the search list.
        """
        searched = [str(p) for p in DEFAULT_LICENSE_PATHS]
        self.assertNotIn(str(Path.home() / ".haql"), searched)
        for p in DEFAULT_LICENSE_PATHS:
            self.assertTrue(str(p).startswith(str(Path.home())))


class TestRevocation(unittest.TestCase):
    """Revocation needs the registry, so it is a separate explicit call."""

    def test_nonce_on_the_list_is_detected(self):
        lic = License("X", "team", 1, "2099-01-01", "haql", "nonce-a")
        self.assertTrue(check_revocations(lic, {"nonce-a"}))

    def test_nonce_absent_from_the_list_is_clean(self):
        lic = License("X", "team", 1, "2099-01-01", "haql", "nonce-a")
        self.assertFalse(check_revocations(lic, {"nonce-b"}))

    def test_empty_nonce_is_never_revoked(self):
        lic = License("X", "team", 1, "2099-01-01", "haql", "")
        self.assertFalse(check_revocations(lic, {"", "anything"}), "no nonce means no claim to revoke")


class TestEmbeddedKey(unittest.TestCase):
    def test_embedded_key_is_32_bytes(self):
        self.assertEqual(len(EMBEDDED_PUBLIC_KEY), 32)

    def test_embedded_key_is_a_valid_curve_point(self):
        """A malformed embedded key would reject every real customer."""
        _L._decode_point(EMBEDDED_PUBLIC_KEY)

    def test_known_tiers_are_documented(self):
        self.assertEqual(TIERS, ("desktop", "team", "enterprise"))


if __name__ == "__main__":
    unittest.main(verbosity=2)