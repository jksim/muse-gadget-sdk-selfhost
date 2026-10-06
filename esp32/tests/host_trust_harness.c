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

/*
 * host_trust_harness CA_FILE|- NOISE_KEY|-
 * Prints host_trust_check()'s answer ("ok" or the status), then, for a key
 * that decodes, "key <hex>".
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "host_trust.h"

static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc((size_t)n + 1);
    if (!buf || fread(buf, 1, (size_t)n, f) != (size_t)n) {
        fclose(f);
        free(buf);
        return NULL;
    }
    buf[n] = '\0';
    fclose(f);
    return buf;
}

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    char *ca = strcmp(argv[1], "-") == 0 ? NULL : read_file(argv[1]);
    if (strcmp(argv[1], "-") != 0 && !ca) return 3;
    const char *key = strcmp(argv[2], "-") == 0 ? NULL : argv[2];
    const char *status = host_trust_check(ca, key);
    printf("%s\n", status ? status : "ok");
    uint8_t raw[HOST_TRUST_NOISE_KEY_BYTES];
    if (key && host_trust_decode_noise_key(key, raw)) {
        printf("key ");
        for (size_t i = 0; i < sizeof(raw); i++) printf("%02x", raw[i]);
        printf("\n");
    }
    free(ca);
    return 0;
}
