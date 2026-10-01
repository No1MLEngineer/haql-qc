"""Signed license verification for commercial builds.

A ``HAQL1`` license is three base64url segments::

    HAQL1.<payload>.<signature>

The payload is a small JSON object naming the customer, the tier, the seat
count, and an expiry date. The signature is Ed25519 over the exact payload
bytes, checked against the public key compiled into this package.

Why the signature matters: without it, a license is just a string the customer
edits. With it, editing the expiry produces a token that fails to verify, and
the failure is specific enough to act on.

What an offline check cannot do
-------------------------------
Revocation is not knowable here. The issuer keeps a registry and can mark a
nonce revoked, but nothing in the token itself records that fact, so a
revoked license verifies correctly until it expires. Revocation therefore
requires online validation against the issuer, and this module does not
pretend otherwise. ``check_revocations`` is provided for callers that do have
a registry available.

Nothing here is secret. The public key ships in the wheel, which is what makes
verification possible without a network call.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

LICENSE_PREFIX = "HAQL1"

# The ed25519 public key that verifies issued licenses.
#
# This is the public half of the issuer keypair held at ~/.haql-issuer/. The
# private half never enters the repository, the wheel, or this process. Every
# license token carries a nonce; see tools/haql_license.py to issue one.
#
# KEY ROTATION: replacing this value invalidates every license already issued
# under the previous key, so rotate deliberately and keep the old public key
# available for re-verification of historical audit reports.
EMBEDDED_PUBLIC_KEY = bytes.fromhex(
    "e04960825188a57cc0d8beb2cfca524119950a9d23aa8eef77613f2dc4ac7cbc"
)

TIERS = ("desktop", "team", "enterprise")

LICENSE_ENV_VAR = "HAQL_LICENSE"
LICENSE_PATH_ENV_VAR = "HAQL_LICENSE_FILE"

# Deliberately NOT ~/.haql/. That directory is not ours to claim: on developer
# machines it holds the `mem` encrypted-memory store (config.json, memory.json,
# runs.jsonl), and in the field it may well hold something else entirely. A tool
# that drops files into a shared dot-directory will eventually collide with
# another tool that owns the same name, and the symptom will look like a corrupt
# license rather than a namespace clash.
DEFAULT_LICENSE_PATHS = (
    Path.home() / ".config" / "haql" / "license.key",
    Path.home() / ".haql-license" / "license.key",
)


class LicenseError(Exception):
    """A license could not be read or parsed at all."""


# --------------------------------------------------------------------------
# Ed25519 verification (RFC 8032), implemented here so the package keeps its
# zero-runtime-dependency guarantee. Roughly 200 lines of field arithmetic is
# a better trade than taking a crypto dependency for one signature check.
# --------------------------------------------------------------------------

_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _Q - 2, _Q) % _Q
_SQRT_M1 = pow(2, (_Q - 1) // 4, _Q)


def _inv(x: int) -> int:
    return pow(x, _Q - 2, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _SQRT_M1) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = 4 * _inv(5)
_BX = _xrecover(_BY)
_B = [_BX % _Q, _BY % _Q, 1, (_BX * _BY) % _Q]


def _pt_add(p: list[int], q: list[int]) -> list[int]:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _Q
    b = (p[1] + p[0]) * (q[1] + q[0]) % _Q
    c = 2 * p[3] * q[3] * _D % _Q
    dd = 2 * p[2] * q[2] % _Q
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return [e * f % _Q, g * h % _Q, f * g % _Q, e * h % _Q]


def _pt_mul(p: list[int], e: int) -> list[int]:
    r = [0, 1, 1, 0]
    while e > 0:
        if e & 1:
            r = _pt_add(r, p)
        p = _pt_add(p, p)
        e >>= 1
    return r


def _pt_equal(p: list[int], q: list[int]) -> bool:
    """Projective equality: the same point under any valid scaling."""
    if p[0] * q[2] % _Q != q[0] * p[2] % _Q:
        return False
    return p[1] * q[2] % _Q == q[1] * p[2] % _Q


def _decode_point(raw: bytes) -> list[int]:
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= _Q:
        raise ValueError("non-canonical y coordinate")
    x = _xrecover(y)
    if x & 1 != (raw[31] >> 7):
        x = _Q - x
    point = [x, y, 1, x * y % _Q]
    if (
        -point[0] * point[0]
        + point[1] * point[1]
        - point[2] * point[2]
        - _D * point[3] * point[3]
    ) % _Q != 0:
        raise ValueError("point is not on the curve")
    return point


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only when signature is a valid Ed25519 signature over message.

    Fails closed on every malformed input rather than raising, because this
    is called on attacker-supplied bytes.
    """
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        a = _decode_point(public_key)
        r = _decode_point(signature[:32])
    except ValueError:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    digest = hashlib.sha512(signature[:32] + public_key + message).digest()
    k = int.from_bytes(digest, "little") % _L
    return _pt_equal(_pt_mul(_B, s), _pt_add(r, _pt_mul(a, k)))


# --------------------------------------------------------------------------
# License parsing
# --------------------------------------------------------------------------


def _b64url_decode(text: str) -> bytes:
    padded = text.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        return base64.b64decode(padded)
    except Exception as exc:  # noqa: BLE001
        raise LicenseError(f"malformed base64url segment: {exc}") from exc


@dataclass
class License:
    """A verified license. Do not construct one without verifying it."""

    company: str
    tier: str
    seats: int
    expires: str
    issuer: str
    nonce: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def unlimited_seats(self) -> bool:
        return self.seats <= 0

    @property
    def expiry_date(self) -> date | None:
        try:
            return date.fromisoformat(self.expires)
        except ValueError:
            return None

    def days_remaining(self, today: date | None = None) -> int | None:
        exp = self.expiry_date
        if exp is None:
            return None
        return (exp - (today or date.today())).days

    def is_expired(self, today: date | None = None) -> bool:
        exp = self.expiry_date
        if exp is None:
            return True
        return exp < (today or date.today())


@dataclass
class LicenseCheck:
    """The outcome of looking for and verifying a license."""

    valid: bool
    source: str | None = None
    license: License | None = None
    reason: str | None = None

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "valid": self.valid,
            "source": self.source,
            "reason": self.reason,
            # Fingerprinting the key lets a support conversation start by
            # answering "which build am I running, and which key does it
            # trust" without exchanging any secret material.
            "public_key_sha256": hashlib.sha256(EMBEDDED_PUBLIC_KEY).hexdigest(),
        }
        if self.license is not None:
            out["company"] = self.license.company
            out["tier"] = self.license.tier
            out["seats"] = self.license.seats
            out["expires"] = self.license.expires
            out["nonce"] = self.license.nonce
            out["days_remaining"] = self.license.days_remaining()
        return out


def parse_token(token: str) -> tuple[dict[str, Any], bytes, bytes]:
    """Split a HAQL1 token into (payload dict, raw payload bytes, signature).

    The raw bytes are returned as well as the parsed dict because the signature
    covers the bytes, not the parse. Re-serialising the dict would change
    spacing and break verification.
    """
    token = token.strip()
    parts = token.split(".")
    if len(parts) != 3:
        raise LicenseError(
            f"malformed license, expected {LICENSE_PREFIX}.<payload>.<signature>"
        )
    prefix, payload_b64, sig_b64 = parts
    if prefix != LICENSE_PREFIX:
        raise LicenseError(
            f"unknown license format {prefix!r}, expected {LICENSE_PREFIX}"
        )
    raw = _b64url_decode(payload_b64)
    signature = _b64url_decode(sig_b64)
    if len(signature) != 64:
        raise LicenseError(f"signature must be 64 bytes, got {len(signature)}")
    # The issuer wrote the payload into a fixed-size buffer, so it can carry
    # trailing NUL padding. The signed message is the JSON up to the first NUL.
    message = raw.split(b"\x00", 1)[0]
    try:
        payload = json.loads(message.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise LicenseError(f"license payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LicenseError("license payload must be a JSON object")
    return payload, message, signature


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise LicenseError(f"license payload missing required field {key!r}")
    return payload[key]


def verify_token(
    token: str,
    public_key: bytes | None = None,
    today: date | None = None,
) -> LicenseCheck:
    """Verify signature, then expiry. Order matters and is deliberate.

    A tampered token must fail on the signature even when its expiry looks
    fine, so the signature is checked first and never skipped.

    ``public_key`` defaults to the embedded key, resolved at call time rather
    than at import time so that replacing the module attribute takes effect.
    """
    key = EMBEDDED_PUBLIC_KEY if public_key is None else public_key
    try:
        payload, message, signature = parse_token(token)
    except LicenseError as exc:
        return LicenseCheck(valid=False, reason=str(exc))

    if not ed25519_verify(key, message, signature):
        return LicenseCheck(
            valid=False,
            reason="signature does not match the embedded license key",
        )

    try:
        company = str(_require(payload, "comp"))
        tier = str(_require(payload, "tier"))
        expires = str(_require(payload, "exp"))
        seats = int(payload.get("seats", 0))
    except (LicenseError, TypeError, ValueError) as exc:
        return LicenseCheck(valid=False, reason=str(exc))

    if tier not in TIERS:
        return LicenseCheck(
            valid=False,
            reason=f"unknown tier {tier!r}, expected one of {', '.join(TIERS)}",
        )

    lic = License(
        company=company,
        tier=tier,
        seats=seats,
        expires=expires,
        issuer=str(payload.get("iss", "")),
        nonce=str(payload.get("nonce", "")),
        payload=payload,
    )
    if lic.expiry_date is None:
        return LicenseCheck(valid=False, reason=f"unparseable expiry {expires!r}")
    if lic.is_expired(today):
        return LicenseCheck(
            valid=False,
            license=lic,
            reason=(
                f"license expired {lic.expires} "
                f"({lic.days_remaining(today)} days ago)"
            ),
        )
    return LicenseCheck(valid=True, license=lic)


def find_license_text(explicit: str | Path | None = None) -> tuple[str | None, str]:
    """Locate license material. Returns (text, source description)."""
    if explicit is not None:
        p = Path(explicit)
        if not p.exists():
            raise LicenseError(f"license file not found: {p}")
        return p.read_text(encoding="utf-8").strip(), str(p)

    from_env = os.environ.get(LICENSE_PATH_ENV_VAR)
    if from_env:
        p = Path(from_env)
        if not p.exists():
            raise LicenseError(
                f"{LICENSE_PATH_ENV_VAR} points at a missing file: {p}"
            )
        return p.read_text(encoding="utf-8").strip(), str(p)

    inline = os.environ.get(LICENSE_ENV_VAR)
    if inline and inline.strip():
        return inline.strip(), f"${LICENSE_ENV_VAR}"

    for candidate in DEFAULT_LICENSE_PATHS:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip(), str(candidate)

    return None, "none"


def check(
    explicit: str | Path | None = None,
    public_key: bytes | None = None,
    today: date | None = None,
) -> LicenseCheck:
    """Find and verify a license. A missing license is a result, not an error.

    Callers that require a license should fail on ``valid is False``; callers
    running a free evaluation can report the reason and continue.
    """
    key = EMBEDDED_PUBLIC_KEY if public_key is None else public_key
    try:
        text, source = find_license_text(explicit)
    except LicenseError as exc:
        return LicenseCheck(valid=False, reason=str(exc))
    if not text:
        return LicenseCheck(
            valid=False,
            reason=(
                f"no license found in any of "
                f"{[str(p) for p in DEFAULT_LICENSE_PATHS]}"
            ),
        )
    result = verify_token(text, public_key=key, today=today)
    result.source = source
    if not result.valid and (result.reason or "").startswith("malformed license"):
        # Say where the junk came from. "malformed license" with no path is
        # unactionable -- the operator cannot tell which of three lookup
        # locations to go and clear.
        result.reason = f"{result.reason} (read from {source})"
    return result


def check_revocations(lic: License, revoked_nonces: set[str]) -> bool:
    """True when the license's nonce is on a revocation list.

    Only meaningful for a caller that has the issuer's registry. Offline
    verification cannot detect revocation; see the module docstring.
    """
    return bool(lic.nonce) and lic.nonce in revoked_nonces