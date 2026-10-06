"""The host's private CA, its TLS server cert, and its Noise static key.

Devices are provisioned with the CA (``ca_cert``) as their only trust anchor
and with the Noise public key (``noise_static_pub``) as a pin.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, x25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
SERVER_DAYS = 365
_BACKDATE = datetime.timedelta(minutes=5)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def create_ca() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("musehost CA"))
        .issuer_name(_name("musehost CA"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def create_server_cert(
    ca_key: ec.EllipticCurvePrivateKey,
    ca: x509.Certificate,
    hostnames: list[str],
    ips: list[str],
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = [x509.DNSName(name) for name in hostnames]
    sans += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(hostnames[0] if hostnames else ips[0]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + datetime.timedelta(days=SERVER_DAYS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def create_noise_key() -> x25519.X25519PrivateKey:
    return x25519.X25519PrivateKey.generate()


def noise_public_b64(key: x25519.X25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def write_private_key(path: Path, key) -> None:
    """Write ``key`` as unencrypted PKCS8 PEM, readable only by the owner."""
    data = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


def write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def load_noise_key(path: Path) -> x25519.X25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, x25519.X25519PrivateKey):
        raise ValueError(f"{path} is not an X25519 key")
    return key
