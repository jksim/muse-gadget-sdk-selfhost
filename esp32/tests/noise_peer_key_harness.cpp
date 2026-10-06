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


// Drives one Noise XX handshake as the initiator against a responder on the
// other end of stdin/stdout (the test runs the SDK's Python responder):
//   out: "msg1 <hex>"          in: "<msg2 hex>"
//   out: "before <hex|empty>"  (peer key before message 2)
//   out: "after <status> <hex|empty>"
// Exit status 0 unless the harness itself fails.

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include <xplat/noise/core/ClientSession.h>
#include <xplat/noise/core/PsaCryptoBackend.h>

namespace tn = musegadgets::noise::core;

namespace {

std::string Hex(tn::ConstByteSpan bytes) {
  if (bytes.size() == 0) {
    return "empty";
  }
  std::string out;
  char buf[3];
  for (size_t i = 0; i < bytes.size(); ++i) {
    std::snprintf(buf, sizeof(buf), "%02x", bytes.data()[i]);
    out += buf;
  }
  return out;
}

bool Unhex(const std::string& text, std::vector<uint8_t>& out) {
  if (text.size() % 2 != 0) {
    return false;
  }
  out.clear();
  for (size_t i = 0; i < text.size(); i += 2) {
    unsigned value = 0;
    if (std::sscanf(text.c_str() + i, "%2x", &value) != 1) {
      return false;
    }
    out.push_back(static_cast<uint8_t>(value));
  }
  return true;
}

}  // namespace

int main() {
  tn::PsaCryptoBackend backend;
  tn::ClientSession session(backend);

  std::vector<uint8_t> msg1(256);
  const tn::StatusWithSize written =
      session.WriteHandshakeMessage1(tn::ByteSpan(msg1.data(), msg1.size()));
  if (!written.ok()) {
    return 1;
  }
  std::printf("msg1 %s\n",
              Hex(tn::ConstByteSpan(msg1.data(), written.size())).c_str());
  std::printf("before %s\n", Hex(session.peerStaticPublicKey()).c_str());
  std::fflush(stdout);

  char line[8192];
  if (std::fgets(line, sizeof(line), stdin) == nullptr) {
    return 2;
  }
  std::string text(line);
  while (!text.empty() && (text.back() == '\n' || text.back() == '\r')) {
    text.pop_back();
  }
  std::vector<uint8_t> msg2;
  if (!Unhex(text, msg2)) {
    return 3;
  }

  std::vector<uint8_t> extra(1024);
  size_t extraWritten = 0;
  const tn::Status read = session.ReadHandshakeMessage2(
      tn::ConstByteSpan(msg2.data(), msg2.size()),
      tn::ByteSpan(extra.data(), extra.size()),
      extraWritten);
  std::printf("after %s %s\n", read.ok() ? "ok" : "failed",
              Hex(session.peerStaticPublicKey()).c_str());
  return 0;
}
