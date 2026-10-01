#!/usr/bin/env python3
"""Issue haql-qc license tokens.

This is the issuer side. It holds the private key and therefore never ships
inside the package -- it lives in this repository, and the wheel contains only
the verifier. Keep it that way.

The private key is read from ~/.haql-issuer/private.key and must never be
committed, copied into the repo, or pasted into a chat. If it leaks, rotate
the keypair: the public half is compiled into every published wheel, so
rotating means every license you ever issued stops verifying.

Usage
-----
    # print a token to stdout
    tools/haql_license.py --company "North Sea Data" --tier team \
        --seats 10 --expires 2027-01-01

    # write it to the customer's file as well
    tools/haql_license.py --company "North Sea Data" --tier team \
        --seats 10 --expires 2027-01-01 --out ~/haql.key

    # append to your revocation registry, then hand the token over
    tools/haql_license.py --company "North Sea Data" ... --registry

    # check one before sending it
    tools/haql_license.py --verify HAQL1.xxx.yyy

Every token gets a random nonce so that two customers on identical terms
receive distinguishable tokens. The nonce is what a revocation registry keys
on, so do not reuse one.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ISSUER = "haql"
TIERS = ("desktop", "team", "enterprise")

BUCKETS = {
    "issued": "issued",
    "revoke": "revoked",
    "reinstate": "reinstated",
}

KEY_DIR = Path(os.environ.get("HAQL_ISSUER_KEY_DIR", Path.home() / ".haql-issuer"))
PRIVATE_KEY = KEY_DIR / "private.key"

def B64U(raw: bytes) -> str:
    """base64url without padding, as the HAQL1 wire format requires."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def load_private_key():
    """Read the raw 32-byte ed25519 seed from disk."""
    if not PRIVATE_KEY.exists():
        sys.exit(
            f"no issuer key at {PRIVATE_KEY}\n"
            "Generate one, or point HAQL_ISSUER_KEY_DIR at the machine that holds it."
        )
    raw = PRIVATE_KEY.read_bytes()
    if len(raw) != 32:
        sys.exit(f"issuer key at {PRIVATE_KEY} is {len(raw)} bytes; expected 32")
    return raw


def sign(seed: bytes, message: bytes) -> bytes:
    """Pure-Python ed25519 signing, so the issuer needs no third-party package.

    RFC 8032 section 5.1.6. Deliberately a separate implementation from the
    verifier's: if they were the same code, a bug in it would cancel out on
    both sides and no local test would notice.
    """
    import hashlib

    q = 2**255 - 19
    ell = 2**252 + 27742317777372353535851937790883648493  # group order
    d = -121665 * pow(121666, q - 2, q) % q
    i = pow(2, (q - 1) // 4, q)

    def xrecover(y: int) -> int:
        xx = (y * y - 1) * pow(d * y * y + 1, q - 2, q)
        x = pow(xx, (q + 3) // 8, q)
        if (x * x - xx) % q != 0:
            x = (x * i) % q
        if x % 2 != 0:
            x = q - x
        return x

    by = 4 * pow(5, q - 2, q) % q
    bx = xrecover(by)
    B = (bx % q, by % q, 1, (bx * by) % q)

    def edwards_add(P, Q):
        (x1, y1, z1, t1), (x2, y2, z2, t2) = P, Q
        a = (y1 - x1) * (y2 - x2) % q
        b = (y1 + x1) * (y2 + x2) % q
        c = t1 * 2 * d * t2 % q
        dd = z1 * 2 * z2 % q
        e, f, g, h = b - a, dd - c, dd + c, b + a
        return (e * f % q, g * h % q, f * g % q, e * h % q)

    def edwards_double(P):
        (x1, y1, z1, _) = P
        a = x1 * x1 % q
        b = y1 * y1 % q
        c = 2 * z1 * z1 % q
        h = a + b
        e = h - (x1 + y1) * (x1 + y1) % q
        g = a - b
        f = c + g
        return (e * f % q, g * h % q, f * g % q, e * h % q)

    def scalarmult(P, e):
        if e == 0:
            return (0, 1, 1, 0)
        Q = scalarmult(P, e // 2)
        Q = edwards_double(Q)
        if e & 1:
            Q = edwards_add(Q, P)
        return Q

    def encodeint(y: int) -> bytes:
        return y.to_bytes(32, "little")

    def encodirect(P) -> bytes:
        (x, y, z, _) = P
        zi = pow(z, q - 2, q)
        x, y = x * zi % q, y * zi % q
        bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
        return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(32))

    def bit(h, i):
        return (h[i // 8] >> (i % 8)) & 1

    h = hashlib.sha512(seed).digest()
    a = 2**254 + sum(2**i * bit(h, i) for i in range(3, 254))
    A = encodirect(scalarmult(B, a))
    prefix = h[32:]

    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % ell
    R = encodirect(scalarmult(B, r))
    k = int.from_bytes(hashlib.sha512(R + A + message).digest(), "little") % ell
    return R + encodeint((r + k * a) % ell)


def build_payload(company: str, tier: str, seats: int, expires: date) -> bytes:
    """Serialise the signed payload.

    Field names are short because the whole thing ends up in a token the
    customer pastes into an environment variable. The verifier requires
    ``comp``, ``tier``, ``exp`` and reads ``seats``, ``iss`` and ``nonce``.
    """
    payload = {
        "comp": company,
        "tier": tier,
        "exp": expires.isoformat(),
        "seats": seats,
        "iss": ISSUER,
        "nonce": secrets.token_hex(16),
    }
    # compact separators: the signature covers these exact bytes
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def issue(company: str, tier: str, seats: int, expires: date) -> str:
    seed = load_private_key()
    if tier not in TIERS:
        sys.exit(f"unknown tier {tier!r}; expected one of {', '.join(TIERS)}")
    message = build_payload(company, tier, seats, expires)
    signature = sign(seed, message)
    return f"HAQL1.{B64U(message)}.{B64U(signature)}"


def decode_payload_bytes(raw: bytes) -> dict:
    """Parse the signed payload from the exact bytes that were signed.

    A leading NUL is tolerated because the original Windows issuer wrote one,
    and refusing to read those tokens would strand customers holding keys that
    still verify.
    """
    return json.loads(raw.split(b"\x00", 1)[0].decode("utf-8"))


def decode_payload(token: str) -> dict:
    """Recover the signed payload dict from a token.

    Kept separate from ``record_event`` so the payload format has one reader,
    and so tests can assert on a payload without minting a real signature.
    """
    payload_b64 = token.strip().split(".")[1]
    raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    return decode_payload_bytes(raw)


def record_event(token: str, path: Path, action: str) -> None:
    """Append the token's nonce to the registry under the matching list.

    Three states matter operationally and each is tracked separately:

    ``issued``    tokens handed out, presumed live
    ``revoked``   tokens that must stop working
    ``reinstated`` tokens revoked and then deliberately restored

    Keeping ``issued`` is what makes the registry useful as a customer record
    rather than only a blacklist. A customer who asks for their key reissued
    can be found by nonce instead of by guesswork.
    """
    payload = decode_payload(token)
    nonce = payload.get("nonce")
    if not nonce:
        sys.exit("token has no nonce; cannot register it")

    path.parent.mkdir(parents=True, exist_ok=True)
    registry: dict = {}
    if path.exists():
        try:
            registry = json.loads(path.read_text())
        except json.JSONDecodeError:
            sys.exit(f"registry at {path} is not valid JSON; refusing to overwrite")

    entry = {
        "nonce": nonce,
        "company": payload.get("comp", ""),
        "tier": payload.get("tier", ""),
        "seats": payload.get("seats", 0),
        "expires": payload.get("exp", ""),
        "date": date.today().isoformat(),
    }
    bucket = BUCKETS[action]
    entries = registry.setdefault(bucket, [])
    if any(e.get("nonce") == nonce for e in entries):
        return
    entries.append(entry)
    _atomic_write(path, json.dumps(registry, indent=2, sort_keys=True) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    A partially written registry is worse than no registry: it looks
    authoritative and cannot be parsed. Rename is atomic on POSIX.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".registry-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Issue haql-qc license tokens.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--company", help="legal name of the licensed company")
    p.add_argument("--tier", choices=TIERS, help="desktop, team, or enterprise")
    p.add_argument("--seats", type=int, help="licensed seats; 0 means unlimited")
    p.add_argument(
        "--expires",
        help="expiry date YYYY-MM-DD, or +N for N days from today",
    )
    p.add_argument("--out", type=Path, help="also write the token to this file")
    p.add_argument(
        "--registry",
        type=Path,
        default=Path.home() / ".haql-issuer" / "revocations.json",
        help="revocation registry to append the nonce to (default: %(default)s)",
    )
    p.add_argument(
        "--revoke",
        help="mark an existing token's nonce revoked in the registry",
    )
    p.add_argument(
        "--reinstate",
        help="reinstate a previously revoked token's nonce",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="print the registry: who holds a live license and who does not",
    )
    p.add_argument("--verify", help="verify a token and print its payload as JSON")
    args = p.parse_args(argv)

    if args.list:
        if not args.registry.exists():
            print("registry is empty", file=sys.stderr)
            return 0
        reg = json.loads(args.registry.read_text())
        print(f"issued    : {len(reg.get('issued', []))}")
        for e in reg.get("issued", []):
            state = "REVOKED" if any(
                r.get("nonce") == e["nonce"] for r in reg.get("revoked", [])
            ) else "live"
            print(f"  [{state:>7}] {e.get('company','')} | {e.get('tier','')} "
                  f"| {e.get('seats','?')} seats | exp {e.get('expires','')} "
                  f"| nonce {e.get('nonce','')[:12]}")
        for e in reg.get("reinstated", []):
            print(f"  [RESTORE] {e.get('company','')} | {e.get('date','')}")
        return 0

    if args.reinstate:
        record_event(args.reinstate, args.registry, "reinstate")
        print(f"reinstated in {args.registry}", file=sys.stderr)
        return 0

    if args.verify:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from haql_qc import licensing

        result = licensing.verify_token(args.verify)
        print(json.dumps(result.summary(), indent=2, sort_keys=True))
        return 0 if result.valid else 1

    if args.revoke:
        record_event(args.revoke, args.registry, "revoke")
        print(f"revoked in {args.registry}", file=sys.stderr)
        return 0

    if not args.company or not args.tier or args.expires is None:
        p.error("--company, --tier and --expires are required to issue a token")

    expires = _parse_expiry(args.expires)
    token = issue(args.company, args.tier, args.seats, expires)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        # 0600: the token is a bearer credential for the whole entitlement.
        fd = os.open(str(args.out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")

    record_event(token, args.registry, "issued")
    print(token)
    return 0


def _parse_expiry(text: str) -> date:
    if text.startswith("+"):
        try:
            days = int(text[1:])
        except ValueError:
            sys.exit(f"malformed expiry {text!r}; expected +N or YYYY-MM-DD")
        return date.today() + timedelta(days=days)
    try:
        return date.fromisoformat(text)
    except ValueError:
        sys.exit(f"malformed expiry {text!r}; expected YYYY-MM-DD or +N")


if __name__ == "__main__":
    raise SystemExit(main())