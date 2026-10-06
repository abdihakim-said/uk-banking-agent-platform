"""Local stand-in for an identity provider: generates an RSA key pair and issues customer tokens.

    python scripts/dev_idp.py keys                       # writes .dev-keys/{private,public}.pem
    python scripts/dev_idp.py token --customer C001      # prints a 30-minute access token

For local development only. In any shared environment, point BANK_OIDC_JWKS_URL at the real IdP.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEY_DIR = Path(".dev-keys")
CUSTOMERS = {"C001": ("Amina", "Yusuf"), "C002": ("Daniel", "Okafor")}


def generate_keys(key_dir: Path = KEY_DIR) -> None:
    key_dir.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (key_dir / "private.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (key_dir / "public.pem").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    print(f"wrote {key_dir}/private.pem and {key_dir}/public.pem")


def issue_token(customer_id: str, private_pem: str, issuer: str = "http://localhost:8000/dev-idp",
                audience: str = "uk-banking-assistant", scope: str = "banking.chat", ttl: int = 1800) -> str:
    now = int(time.time())
    given, family = CUSTOMERS.get(customer_id, ("Test", "User"))
    claims = {"iss": issuer, "aud": audience, "sub": f"user-{customer_id.lower()}", "iat": now, "exp": now + ttl,
              "scope": scope, "customer_id": customer_id, "given_name": given, "family_name": family}
    return jwt.encode(claims, private_pem, algorithm="RS256")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("keys")
    tok = sub.add_parser("token")
    tok.add_argument("--customer", default="C001")
    args = parser.parse_args()
    if args.command == "keys":
        generate_keys()
    else:
        print(issue_token(args.customer, (KEY_DIR / "private.pem").read_text()))


if __name__ == "__main__":
    main()
