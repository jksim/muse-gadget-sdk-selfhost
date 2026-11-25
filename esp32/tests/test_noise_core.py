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

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "components" / "noise_core"
EMBEDDED_CXX_FLAGS = ["-fno-exceptions", "-fno-rtti"]


def _cxx_command() -> list[str]:
    cmd = shlex.split(os.environ.get("CXX", "c++"))
    if not cmd or shutil.which(cmd[0]) is None:
        raise unittest.SkipTest("C++ compiler not available")
    return cmd


def _psa_crypto_flags(cxx: list[str], tmp: Path) -> list[str]:
    flags: list[str] | None = None
    if shutil.which("pkg-config") is not None:
        proc = subprocess.run(
            ["pkg-config", "--cflags", "--libs", "mbedcrypto"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode == 0:
            flags = shlex.split(proc.stdout)
    if flags is None:
        flags = ["-lmbedcrypto"]

    probe = tmp / "psa_crypto_probe"
    probe_proc = subprocess.run(
        [
            *cxx,
            "-x",
            "c++",
            "-std=c++17",
            "-o",
            str(probe),
            "-",
            *flags,
        ],
        input=(
            "#include <psa/crypto.h>\n"
            "int main() { return psa_crypto_init() == PSA_SUCCESS ? 0 : 1; }\n"
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if probe_proc.returncode != 0:
        raise unittest.SkipTest(
            "PSA Crypto development headers/libraries not available: "
            + probe_proc.stderr.strip()
        )
    return flags


class NoiseCoreCompileTest(unittest.TestCase):
    def test_imported_core_compiles_and_links_with_psa_crypto(self) -> None:
        cxx = _cxx_command()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            psa_crypto_flags = _psa_crypto_flags(cxx, tmp)
            binary = tmp / "noise_core_harness"
            srcs = [
                COMPONENT / "src" / "ClientSession.cpp",
                COMPONENT / "src" / "InitiatorHandshake.cpp",
                COMPONENT / "src" / "PsaCryptoBackend.cpp",
                COMPONENT / "src" / "ServiceCodec.cpp",
                COMPONENT / "src" / "Status.cpp",
                COMPONENT / "src" / "Transport.cpp",
                COMPONENT / "src" / "TransportFrameCodec.cpp",
            ]
            compile_cmd = [
                *cxx,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                *EMBEDDED_CXX_FLAGS,
                "-g",
                "-O1",
                "-I",
                str(COMPONENT / "include"),
                str(ROOT / "tests" / "noise_core_harness.cpp"),
                *(str(src) for src in srcs),
                "-pthread",
                *psa_crypto_flags,
                "-o",
                str(binary),
            ]
            compile_proc = subprocess.run(
                compile_cmd,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(
                compile_proc.returncode,
                0,
                msg=compile_proc.stdout + compile_proc.stderr,
            )

            run_proc = subprocess.run(
                [str(binary)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(
                run_proc.returncode,
                0,
                msg=run_proc.stdout + run_proc.stderr,
            )

    def test_noise_xx_vector_and_x25519_both_backends(self) -> None:
        """cacophony's Noise_XX_25519_AESGCM_SHA256 vector and RFC 7748 through
        the backend, with X25519 from PSA and from HACL* (NOISE_CORE_X25519_HACL,
        built from ESP-IDF's Everest copy, so it needs IDF_PATH)."""
        cxx = _cxx_command()
        cc = shlex.split(os.environ.get("CC", "cc"))
        if not shutil.which(cc[0]):
            self.skipTest("C compiler not available")
        idf_path = os.environ.get("IDF_PATH")
        if not idf_path:
            self.skipTest("Set IDF_PATH to build HACL* Curve25519 from ESP-IDF")
        everest = Path(idf_path) / "components/mbedtls/mbedtls/tf-psa-crypto/drivers/everest"
        if not (everest / "library/legacy/Hacl_Curve25519.c").exists():
            self.skipTest(f"no HACL* Curve25519 in {idf_path}")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            flags = _psa_crypto_flags(cxx, tmp)
            for mode in ("psa", "hacl"):
                extra: list[str] = []
                if mode == "hacl":
                    obj = tmp / "x25519_hacl.o"
                    proc = subprocess.run(
                        [*cc, "-c", "-O1", "-Wall", "-Wno-unused-function",
                         "-I", str(everest / "library"),
                         "-I", str(everest / "include/tf-psa-crypto/private/everest"),
                         "-I", str(everest / "include/tf-psa-crypto/private/everest/kremlib"),
                         str(COMPONENT / "src" / "X25519Hacl.c"), "-o", str(obj)],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                    extra = [str(obj), "-DNOISE_CORE_X25519_HACL=1"]
                binary = tmp / f"noise_xx_{mode}"
                proc = subprocess.run(
                    [*cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", *EMBEDDED_CXX_FLAGS, "-O1",
                     "-I", str(COMPONENT / "include"),
                     str(ROOT / "tests" / "noise_xx_vector_harness.cpp"),
                     str(COMPONENT / "src" / "PsaCryptoBackend.cpp"),
                     str(COMPONENT / "src" / "Status.cpp"),
                     *extra, *flags, "-o", str(binary)],
                    cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
                run = subprocess.run([str(binary)], text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
                with self.subTest(mode):
                    self.assertEqual(run.returncode, 0, msg=run.stdout + run.stderr)
                    self.assertIn("3 handshake + 3 transport messages and the handshake hash match", run.stdout)
                    self.assertRegex(run.stdout, r"x25519: (\d+)/\1 checks pass")

LINUX_SRC = ROOT.parent / "linux" / "src"
CORE_SRCS = [
    "ClientSession.cpp",
    "InitiatorHandshake.cpp",
    "PsaCryptoBackend.cpp",
    "ServiceCodec.cpp",
    "Status.cpp",
    "Transport.cpp",
    "TransportFrameCodec.cpp",
]


class NoisePeerStaticKeyTest(unittest.TestCase):
    """ClientSession reports the responder's static key once message 2 is read.

    The responder is the SDK's Python NoiseXXResponder, the one self-hosted
    hosts run, so this also checks the two implementations interoperate.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cxx = _cxx_command()
        try:
            import sys

            sys.path.insert(0, str(LINUX_SRC))
            from cryptography.hazmat.primitives.asymmetric import x25519  # noqa: F401
            from musegadget.noise.noise_xx import NoiseXXResponder  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"Python Noise responder not available: {exc}")
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        flags = _psa_crypto_flags(cxx, tmp)
        cls.binary = tmp / "noise_peer_key_harness"
        proc = subprocess.run(
            [
                *cxx,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                *EMBEDDED_CXX_FLAGS,
                "-g",
                "-O1",
                "-I",
                str(COMPONENT / "include"),
                str(ROOT / "tests" / "noise_peer_key_harness.cpp"),
                *(str(COMPONENT / "src" / src) for src in CORE_SRCS),
                "-pthread",
                *flags,
                "-o",
                str(cls.binary),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            cls._tmp.cleanup()
            raise AssertionError(proc.stdout + proc.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def handshake(self, tamper: bool = False) -> tuple[str, str, str]:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import x25519
        from musegadget.noise.noise_xx import NoiseXXResponder

        static = x25519.X25519PrivateKey.generate()
        expected = static.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()
        proc = subprocess.Popen(
            [str(self.binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            kind, msg1 = proc.stdout.readline().split()
            self.assertEqual(kind, "msg1")
            before = proc.stdout.readline().split()
            self.assertEqual(before[0], "before")
            responder = NoiseXXResponder(static_private_key=static)
            responder.initialize()
            msg2 = bytearray(responder.read_message1_and_write_message2(bytes.fromhex(msg1)))
            if tamper:
                msg2[-1] ^= 0x01
            out, _ = proc.communicate(msg2.hex() + "\n", timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0)
        after = out.split()
        self.assertEqual(after[0], "after")
        return before[1], " ".join(after[1:]), expected

    def test_peer_key_is_the_responders_static_key_after_message_2(self) -> None:
        before, after, expected = self.handshake()
        self.assertEqual(before, "empty")
        self.assertEqual(after, f"ok {expected}")

    def test_a_tampered_message_2_leaves_no_peer_key(self) -> None:
        _, after, _ = self.handshake(tamper=True)
        self.assertEqual(after, "failed empty")


if __name__ == "__main__":
    unittest.main()
