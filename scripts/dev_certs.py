"""Generate a development CA and one certificate per service for mutual TLS.

    python scripts/dev_certs.py                        # writes .dev-certs/<service>/{tls.crt,tls.key,ca.crt}
    python scripts/dev_certs.py --namespace bank       # also valid for the kind release's service names

Each certificate is valid for both server and client authentication, and its names cover the docker compose service
name and the Kubernetes service names. Local development only: in a cluster, use cert-manager with your internal CA
(tls.certManager in the Helm chart). Key files are world-readable so containers running as other users can read
them, which is fine for throwaway dev keys and wrong anywhere else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SERVICES = ["web", "gateway", "agent", "demo-idp", "accounts-mcp", "transactions-mcp", "service-mcp", "redis"]


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Banking Assistant Dev"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def generate(out: Path, fullname: str, namespace: str) -> None:
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("banking-assistant-dev-ca")).issuer_name(_name("banking-assistant-dev-ca"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    out.mkdir(parents=True, exist_ok=True)
    (out / "ca.crt").write_bytes(ca_pem)

    for svc in SERVICES:
        k8s = f"{fullname}-{svc}"
        names = [svc, k8s, f"{k8s}.{namespace}", f"{k8s}.{namespace}.svc", f"{k8s}.{namespace}.svc.cluster.local",
                 "localhost"]
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (x509.CertificateBuilder().subject_name(_name(svc)).issuer_name(ca.subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=365))
                .add_extension(x509.SubjectAlternativeName([*(x509.DNSName(n) for n in names),
                                                            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                               critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                      ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(ca_key, hashes.SHA256()))
        d = out / svc
        d.mkdir(exist_ok=True)
        (d / "tls.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (d / "tls.key").write_bytes(_pem_key(key))
        (d / "ca.crt").write_bytes(ca_pem)
        for f in d.iterdir():
            f.chmod(0o644)
    print(f"wrote CA and {len(SERVICES)} service certificates to {out}/")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path(".dev-certs"))
    parser.add_argument("--fullname", default="bank-uk-banking-agent-platform", help="Helm release fullname")
    parser.add_argument("--namespace", default="bank")
    args = parser.parse_args()
    generate(args.out, args.fullname, args.namespace)


if __name__ == "__main__":
    main()
