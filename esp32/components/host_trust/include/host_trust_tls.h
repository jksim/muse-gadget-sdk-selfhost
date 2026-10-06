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
 * TLS trust for connections that may go to a self-hosted Muse. Kept apart from
 * host_trust.h so code without ESP-IDF's TLS headers can use that one.
 */

#include "esp_http_client.h"
#include "esp_tls.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Sets the trust for an HTTP request to cfg->url: the provisioned CA when the
 * URL's host is the paired self-hosted Muse, else the public bundle.
 */
void host_trust_apply_http(esp_http_client_config_t *cfg);

/* The same for a raw TLS connection to `host`. */
void host_trust_apply_tls(esp_tls_cfg_t *cfg, const char *host);

#ifdef __cplusplus
}
#endif
