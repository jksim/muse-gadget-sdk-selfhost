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

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"

static const char *TAG = "host_trust";
/* The pairing's NVS namespace (main/config_store.c). */
static const char *NS = "homehub";

static SemaphoreHandle_t s_lock;
static char *s_ca;   /* PEM, NUL-terminated; NULL when not provisioned */
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

void host_trust_reload(void) {
    char *ca = NULL, *key_text = NULL;
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) == ESP_OK) {
        ca = read_str(h, HOST_TRUST_CA_KEY);
        key_text = read_str(h, HOST_TRUST_NOISE_KEY);
        nvs_close(h);
    }
    uint8_t key[HOST_TRUST_NOISE_KEY_BYTES];
    bool has_key = key_text && host_trust_decode_noise_key(key_text, key);
    if (key_text && !has_key) {
        ESP_LOGW(TAG, "stored Noise key is unusable; not pinning");
    }
    free(key_text);

    lock();
    free(s_ca);
    s_ca = ca;
    s_has_key = has_key;
    memcpy(s_key, key, sizeof(s_key));
    unlock();
    ESP_LOGI(TAG, "host CA %s, Noise key %s", ca ? "provisioned" : "not provisioned",
             has_key ? "pinned" : "not pinned");
}
