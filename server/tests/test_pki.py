import base64
import ipaddress

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from musehost import pki


def test_the_ca_is_an_ec_p256_ca_valid_for_ten_years():
    _, ca = pki.create_ca()
    assert isinstance(ca.public_key(), ec.EllipticCurvePublicKey)
    assert ca.public_key().curve.name == "secp256r1"
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    lifetime = ca.not_valid_after_utc - ca.not_valid_before_utc
    assert 3650 <= lifetime.days <= 3660


def test_the_server_cert_is_signed_by_the_ca_and_names_every_san():
    ca_key, ca = pki.create_ca()
    _, cert = pki.create_server_cert(
        ca_key, ca, ["musehost.local", "musehost.lan"], ["192.168.1.10"]
    )
    cert.verify_directly_issued_by(ca)
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sans.get_values_for_type(x509.DNSName) == ["musehost.local", "musehost.lan"]
    assert sans.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("192.168.1.10")]
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert 365 <= lifetime.days <= 367


def test_the_noise_public_key_is_32_bytes_of_unpadded_base64url():
    encoded = pki.noise_public_b64(pki.create_noise_key())
    assert "=" not in encoded
    assert len(base64.urlsafe_b64decode(encoded + "=")) == 32


def test_keys_round_trip_through_pem_files(tmp_path):
    key = pki.create_noise_key()
    pki.write_private_key(tmp_path / "noise_static.key", key)
    assert pki.noise_public_b64(
        pki.load_noise_key(tmp_path / "noise_static.key")
    ) == pki.noise_public_b64(key)
