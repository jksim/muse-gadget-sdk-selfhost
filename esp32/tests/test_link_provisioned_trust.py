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
"""Pairing hands a self-hosted Muse's CA and Noise key to the firmware.

`provision_v2` may carry `ca_cert` and `noise_static_pub`. They're checked
before provisioning starts, stored before the first call to the host (so it
already uses the CA), and freed like the other credentials.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_C = ROOT / "main" / "app.c"
BLE_SERVER_C = ROOT / "main" / "ble_server.c"
BLE_SERVER_H = ROOT / "main" / "ble_server.h"


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[brace + 1 : i]
    raise AssertionError(f"unterminated {signature}")


class ProvisionedTrustContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = APP_C.read_text()
        self.ble = BLE_SERVER_C.read_text()

    def test_the_callback_carries_both_fields(self) -> None:
        header = BLE_SERVER_H.read_text()
        callback = header[header.index("typedef void (*ble_provision_cb)") :]
        callback = callback[: callback.index(";")]
        self.assertIn("const char *ca_cert", callback)
        self.assertIn("const char *noise_static_pub", callback)

    def test_invalid_trust_is_refused_before_provisioning_starts(self) -> None:
        handler = self.ble[self.ble.index('strcmp(act, "provision_v2") == 0) {') :]
        check = handler.index("provision_trust_error(root)")
        self.assertLess(check, handler.index("xTaskCreate(provision_task"))
        self.assertLess(check, handler.index("calloc(1, sizeof(*a))"))
        body = _function_body(self.ble, "static const char *provision_trust_error(")
        self.assertIn('"ca_cert"', body)
        self.assertIn('"noise_static_pub"', body)
        self.assertIn("host_trust_check(", body)
        # A field that isn't a string is as unusable as a bad value.
        self.assertIn('"error_invalid_ca"', body)
        self.assertIn('"error_invalid_noise_key"', body)

    def test_both_fields_are_freed_like_credentials(self) -> None:
        self.assertEqual(self.ble.count("secure_free_str(a->ca_cert);"), 2)
        self.assertEqual(self.ble.count("secure_free_str(a->noise_static_pub);"), 2)

    def test_trust_is_stored_after_wifi_and_before_the_first_host_call(self) -> None:
        body = _function_body(self.app, "static void on_provision(")
        store = body.index("store_host_trust(ca_cert, noise_static_pub)")
        self.assertLess(body.index("remember_joined_wifi(ssid, password);"), store)
        self.assertLess(store, body.index("accept_pairing_credentials("))
        after = body[store:]
        self.assertLess(after.index('"error_storage"'), after.index("accept_pairing_credentials("))


class HostConnectionTrustContractTest(unittest.TestCase):
    """Connections to the paired host go through host_trust; others keep the bundle."""

    HOST_SITES = {
        "main/vm_api.c": "host_trust_apply_http(&cfg);",
        "main/noise_control.cpp": "host_trust_apply_tls(&cfg, s_noise_host);",
        "components/muse/muse_chat_session.cpp": "host_trust_apply_tls(&cfg, s_host);",
    }
    PUBLIC_SITES = (
        "main/image_fetch.c",
        "main/ota.c",
        "components/muse/muse_account_api.c",  # always Muse's own api.muse.ai
    )

    def test_host_sites_use_host_trust(self) -> None:
        for path, call in self.HOST_SITES.items():
            with self.subTest(path=path):
                source = (ROOT / path).read_text()
                self.assertIn(call, source)
                self.assertIn('#include "host_trust_tls.h"', source)
                self.assertNotIn("crt_bundle_attach = esp_crt_bundle_attach", source)

    def test_public_sites_keep_the_bundle(self) -> None:
        for path in self.PUBLIC_SITES:
            with self.subTest(path=path):
                source = (ROOT / path).read_text()
                self.assertIn("esp_crt_bundle_attach", source)
                self.assertNotIn("host_trust_apply", source)


class StoreHostTrustTest(unittest.TestCase):
    """Runs the real store_host_trust() against fake NVS."""

    def test_store_and_erase(self) -> None:
        cc = shlex.split(os.environ.get("CC", "cc"))
        if not cc or shutil.which(cc[0]) is None:
            self.skipTest("C compiler not available")
        app = APP_C.read_text()
        signature = "static bool store_host_trust(const char *ca_cert, const char *noise_static_pub)"
        production = signature + " {" + _function_body(app, signature) + "}\n"
        harness = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "host_trust.h"
static char ca[8], key[8];
static int reloads;
static const char *fail_key;
static char *slot(const char *k) {
    if (!strcmp(k, HOST_TRUST_CA_KEY)) return ca;
    assert(!strcmp(k, HOST_TRUST_NOISE_KEY));
    return key;
}
static bool config_set_str(const char *k, const char *v) {
    if (fail_key && !strcmp(k, fail_key)) return false;
    snprintf(slot(k), 8, "%s", v);
    return true;
}
static bool config_erase_key(const char *k) {
    if (fail_key && !strcmp(k, fail_key)) return false;
    slot(k)[0] = '\0';
    return true;
}
void host_trust_reload(void) { reloads++; }
@PRODUCTION@
int main(void) {
    assert(store_host_trust("CA", "KEY"));
    assert(!strcmp(ca, "CA") && !strcmp(key, "KEY") && reloads == 1);
    assert(store_host_trust("", NULL));
    assert(!ca[0] && !key[0] && reloads == 2);
    fail_key = HOST_TRUST_NOISE_KEY;
    assert(!store_host_trust("CA", "KEY"));
    assert(reloads == 3);  /* the cache follows NVS even after a failure */
    puts("ok");
    return 0;
}
""".replace("@PRODUCTION@", production)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "store_host_trust.c"
            src.write_text(harness)
            binary = Path(tmp) / "store_host_trust"
            proc = subprocess.run(
                [*cc, "-std=c11", "-Wall", "-Werror", "-I",
                 str(ROOT / "components" / "host_trust" / "include"),
                 str(src), "-o", str(binary)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            run = subprocess.run([str(binary)], text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            self.assertEqual((run.returncode, run.stdout.strip()), (0, "ok"), run.stderr)


if __name__ == "__main__":
    unittest.main()
