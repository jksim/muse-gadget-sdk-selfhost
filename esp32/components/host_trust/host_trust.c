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
#include "host_trust_tls.h"

#include <stdlib.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"

static const char *TAG = "host_trust";
/* The pairing's NVS namespace (main/config_store.c). */
static const char *NS = "homehub";

static SemaphoreHandle_t s_lock;
/*
 * TLS configs point at the CA rather than copy it, so its buffer is allocated
 * once and never freed; a re-pair overwrites it in place.
 */
static char *s_ca_buf;
static bool s_has_ca;
static char s_noise_host[128];
static char s_api_url[256];
static bool s_has_key;
static uint8_t s_key[HOST_TRUST_NOISE_KEY_BYTES];

static void lock(void) {
    if (!s_lock) {
        static StaticSemaphore_t storage;
        s_lock = xSemaphoreCreateMutexStatic(&storage);
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
}

static void unlock(void) { xSemaphoreGive(s_lock); }

static char *read_str(nvs_handle_t h, const char *key) {
    size_t len = 0;
    if (nvs_get_str(h, key, NULL, &len) != ESP_OK || len <= 1) return NULL;
    char *out = malloc(len);
    if (out && nvs_get_str(h, key, out, &len) != ESP_OK) {
        free(out);
        out = NULL;
    }
    return out;
}

static void read_into(nvs_handle_t h, const char *key, char *out, size_t cap) {
    size_t len = cap;
    if (nvs_get_str(h, key, out, &len) != ESP_OK) out[0] = '\0';
}

void host_trust_reload(void) {
    char *ca = NULL, *key_text = NULL;
    char noise_host[sizeof(s_noise_host)] = "", api_url[sizeof(s_api_url)] = "";
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) == ESP_OK) {
        ca = read_str(h, HOST_TRUST_CA_KEY);
        key_text = read_str(h, HOST_TRUST_NOISE_KEY);
        read_into(h, "noise_host", noise_host, sizeof(noise_host));
        read_into(h, "api_url_v2", api_url, sizeof(api_url));
        nvs_close(h);
    }
    if (ca && strlen(ca) > HOST_TRUST_CA_MAX) {
        free(ca);
        ca = NULL;
    }
    if (ca && !s_ca_buf) {
        s_ca_buf = malloc(HOST_TRUST_CA_MAX + 1);
        if (!s_ca_buf) {
            ESP_LOGE(TAG, "no memory for the host CA");
            free(ca);
            ca = NULL;
        }
    }
    uint8_t key[HOST_TRUST_NOISE_KEY_BYTES];
    bool has_key = key_text && host_trust_decode_noise_key(key_text, key);
    if (key_text && !has_key) {
        ESP_LOGW(TAG, "stored Noise key is unusable; not pinning");
    }
    free(key_text);

    lock();
    s_has_ca = ca != NULL;
    if (ca) strcpy(s_ca_buf, ca);
    strcpy(s_noise_host, noise_host);
    strcpy(s_api_url, api_url);
    s_has_key = has_key;
    memcpy(s_key, key, sizeof(s_key));
    unlock();
    free(ca);
    ESP_LOGI(TAG, "host CA %s, Noise key %s", ca ? "provisioned" : "not provisioned",
             has_key ? "pinned" : "not pinned");
}

/* The provisioned CA for `host`, or NULL for the public bundle. */
static const char *ca_for(const char *host) {
    lock();
    const char *ca = host_trust_select(s_has_ca ? s_ca_buf : NULL, host, s_noise_host, s_api_url);
    unlock();
    ESP_LOGD(TAG, "TLS to %s: %s", host ? host : "?", ca ? "host CA" : "public bundle");
    return ca;
}

void host_trust_apply_http(esp_http_client_config_t *cfg) {
    char host[256];
    const char *ca = host_trust_host_of_url(cfg->url, host, sizeof(host)) ? ca_for(host) : NULL;
    if (ca) {
        cfg->cert_pem = ca;
        cfg->cert_len = 0;  /* PEM: measured with strlen */
        cfg->crt_bundle_attach = NULL;
    } else {
        cfg->cert_pem = NULL;
        cfg->crt_bundle_attach = esp_crt_bundle_attach;
    }
}

void host_trust_apply_tls(esp_tls_cfg_t *cfg, const char *host) {
    const char *ca = ca_for(host);
    if (ca) {
        cfg->cacert_buf = (const unsigned char *)ca;
        cfg->cacert_bytes = strlen(ca) + 1;  /* PEM length counts the NUL */
        cfg->crt_bundle_attach = NULL;
    } else {
        cfg->cacert_buf = NULL;
        cfg->cacert_bytes = 0;
        cfg->crt_bundle_attach = esp_crt_bundle_attach;
    }
}
