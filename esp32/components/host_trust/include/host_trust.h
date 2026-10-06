/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

/*
 * A self-hosted Muse provisions its own trust at pairing, over the encrypted
 * Bluetooth channel the owner confirms with the button: the PEM of its CA
 * (`ca_cert`) and its Noise static public key (`noise_static_pub`, 32 bytes as
 * unpadded base64url). With neither, the device trusts the public bundle and
 * doesn't pin the Noise key, as it always has.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* NVS keys, in the same namespace as the rest of the pairing. */
#define HOST_TRUST_CA_KEY "host_ca"
#define HOST_TRUST_NOISE_KEY "noise_pub"

/* An NVS string holds at most 4000 bytes; a CA certificate is about 1 KB. */
#define HOST_TRUST_CA_MAX 4000
#define HOST_TRUST_NOISE_KEY_BYTES 32

/*
 * Checks provisioned trust before anything is stored. NULL or "" means not
 * provisioned. Returns NULL when both are usable, else the pairing status to
 * send: "error_invalid_ca" or "error_invalid_noise_key".
 */
const char *host_trust_check(const char *ca_pem, const char *noise_pub);

/*
 * Loads the stored CA and Noise key into memory for the connections that use
 * them. Call at boot and after the pairing stores or clears them.
 */
void host_trust_reload(void);

/* Decodes a `noise_static_pub` value; false unless it's exactly 32 bytes. */
bool host_trust_decode_noise_key(const char *text, uint8_t out[HOST_TRUST_NOISE_KEY_BYTES]);

/*
 * The host part of "scheme://host[:port]/path" (IPv6 without brackets).
 * False for anything else, or when it doesn't fit in `cap`.
 */
bool host_trust_host_of_url(const char *url, char *out, size_t cap);

/*
 * The CA a connection to `host` should trust: `ca` when one is provisioned and
 * `host` is the paired host (`noise_host`, or the host of `api_url`), compared
 * without case. NULL means the public bundle, for every other host.
 */
const char *host_trust_select(const char *ca, const char *host,
                              const char *noise_host, const char *api_url);

#ifdef __cplusplus
}
#endif
