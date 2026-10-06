# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""host_trust: checking the CA and Noise key a self-hosted Muse provisions."""

from __future__ import annotations

import base64
import datetime
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "components" / "host_trust"


def _cc() -> list[str]:
    cmd = shlex.split(os.environ.get("CC", "cc"))
    if not cmd or shutil.which(cmd[0]) is None:
        raise unittest.SkipTest("C compiler not available")
    return cmd


def _make_ca() -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "musehost test CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class HostTrustCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            import cryptography  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"cryptography not available: {exc}")
        cc = _cc()
        # mbedTLS outside the default paths, e.g. "-I<prefix>/include -L<prefix>/lib".
        extra = shlex.split(os.environ.get("HOST_MBEDTLS_FLAGS", ""))
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.binary = cls.tmp / "host_trust_harness"
        proc = subprocess.run(
            [
                *cc,
                *extra,
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(COMPONENT / "include"),
                str(ROOT / "tests" / "host_trust_harness.c"),
                str(COMPONENT / "host_trust_check.c"),
                "-lmbedx509",
                "-lmbedcrypto",
                "-o",
                str(cls.binary),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            cls._tmp.cleanup()
            if "mbedtls/x509_crt.h" in proc.stderr or "-lmbedx509" in proc.stderr:
                raise unittest.SkipTest("mbedTLS X.509 development files not available")
            raise AssertionError(proc.stdout + proc.stderr)
        cls.ca = _make_ca()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def run_check(self, ca: str | None, key: str | None) -> list[str]:
        ca_arg = "-"
        if ca is not None:
            path = self.tmp / "ca.pem"
            path.write_text(ca)
            ca_arg = str(path)
        proc = subprocess.run(
            [str(self.binary), ca_arg, "-" if key is None else key],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.split("\n")[:-1]

    def test_nothing_provisioned_is_fine(self) -> None:
        self.assertEqual(self.run_check(None, None), ["ok"])
        self.assertEqual(self.run_check("", ""), ["ok"])

    def test_a_real_ca_and_key_are_accepted_and_the_key_decodes(self) -> None:
        raw = bytes(range(32))
        self.assertEqual(self.run_check(self.ca, _b64url(raw)), ["ok", f"key {raw.hex()}"])

    def test_a_ca_that_is_not_a_certificate_is_refused(self) -> None:
        broken = self.ca.replace(self.ca.splitlines()[3], "A" * 64)
        for ca in ("hello", "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", broken):
            with self.subTest(ca=ca[:30]):
                self.assertEqual(self.run_check(ca, None), ["error_invalid_ca"])

    def test_a_ca_over_4000_bytes_is_refused(self) -> None:
        chain = self.ca * (4001 // len(self.ca) + 1)
        self.assertGreater(len(chain), 4000)
        self.assertEqual(self.run_check(chain, None), ["error_invalid_ca"])

    def test_keys_that_are_not_exactly_32_bytes_of_unpadded_base64url_are_refused(self) -> None:
        good = _b64url(bytes(range(32)))
        for key in (
            _b64url(bytes(31)),
            _b64url(bytes(33)),
            good + "=",
            good[:-1] + "+",
            good[:-1] + "B",  # non-zero unused bits
            "",  # empty means absent: fine on its own
        ):
            with self.subTest(key=key):
                expected = ["ok"] if key == "" else ["error_invalid_noise_key"]
                self.assertEqual(self.run_check(None, key), expected)

    def test_the_ca_is_checked_before_the_key(self) -> None:
        self.assertEqual(self.run_check("nope", "nope"), ["error_invalid_ca"])


if __name__ == "__main__":
    unittest.main()
