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

#include "host_trust.h"

#include <string.h>
#include <strings.h>

#include "mbedtls/x509_crt.h"

static int b64url_value(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '-') return 62;
    if (c == '_') return 63;
    return -1;
}

bool host_trust_decode_noise_key(const char *text, uint8_t out[HOST_TRUST_NOISE_KEY_BYTES]) {
    /* 32 bytes are 43 characters unpadded; the last carries 2 unused bits. */
    if (!text || strlen(text) != 43) return false;
    uint32_t acc = 0;
    int bits = 0;
    size_t n = 0;
    for (size_t i = 0; i < 43; i++) {
        int v = b64url_value(text[i]);
        if (v < 0) return false;
        acc = (acc << 6) | (uint32_t)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out[n++] = (uint8_t)(acc >> bits);
        }
    }
    /* Only the canonical encoding: the leftover bits must be zero. */
    return n == HOST_TRUST_NOISE_KEY_BYTES && (acc & ((1u << bits) - 1)) == 0;
}

static bool ca_usable(const char *pem) {
    size_t len = strlen(pem);
    if (len > HOST_TRUST_CA_MAX) return false;
    mbedtls_x509_crt crt;
    mbedtls_x509_crt_init(&crt);
    /* PEM parsing wants the terminating NUL counted in the length. */
    int rc = mbedtls_x509_crt_parse(&crt, (const unsigned char *)pem, len + 1);
    bool ok = rc == 0 && crt.version != 0;
    mbedtls_x509_crt_free(&crt);
    return ok;
}

const char *host_trust_check(const char *ca_pem, const char *noise_pub) {
    if (ca_pem && *ca_pem && !ca_usable(ca_pem)) {
        return "error_invalid_ca";
    }
    uint8_t key[HOST_TRUST_NOISE_KEY_BYTES];
    if (noise_pub && *noise_pub && !host_trust_decode_noise_key(noise_pub, key)) {
        return "error_invalid_noise_key";
    }
    return NULL;
}

bool host_trust_host_of_url(const char *url, char *out, size_t cap) {
    if (!url || !out || cap == 0) return false;
    const char *start = strstr(url, "://");
    if (!start) return false;
    start += 3;
    const char *end;
    if (*start == '[') {
        start++;
        end = strchr(start, ']');
        if (!end) return false;
    } else {
        end = start + strcspn(start, ":/?#");
    }
    size_t len = (size_t)(end - start);
    if (len == 0 || len >= cap) return false;
    memcpy(out, start, len);
    out[len] = '\0';
    return true;
}

static bool is_paired_host(const char *host, const char *noise_host, const char *api_url) {
    if (!host || !*host) return false;
    if (noise_host && *noise_host && strcasecmp(host, noise_host) == 0) return true;
    char api_host[256];
    return host_trust_host_of_url(api_url, api_host, sizeof(api_host))
        && strcasecmp(host, api_host) == 0;
}

const char *host_trust_select(const char *ca, const char *host,
                              const char *noise_host, const char *api_url) {
    if (!ca || !*ca) return NULL;
    return is_paired_host(host, noise_host, api_url) ? ca : NULL;
}

bool host_trust_pin_ok(const uint8_t *pin, const char *host, const char *noise_host,
                       const char *api_url, const uint8_t *key, size_t key_len) {
    if (!pin) return true;
    if (host && *host && !is_paired_host(host, noise_host, api_url)) return true;
    if (!key || key_len != HOST_TRUST_NOISE_KEY_BYTES) return false;
    uint8_t diff = 0;
    for (size_t i = 0; i < HOST_TRUST_NOISE_KEY_BYTES; i++) diff |= (uint8_t)(pin[i] ^ key[i]);
    return diff == 0;
}
