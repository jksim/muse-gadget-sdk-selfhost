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

"""The self-host overlay accepts the host's addresses; stock builds do not."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPTION = "CONFIG_HOMEHUB_CUSTOM_SERVER_HOSTS=y"


class SelfhostOverlayTest(unittest.TestCase):
    def test_the_selfhost_overlay_accepts_any_server_host(self) -> None:
        lines = (ROOT / "devices/sdkconfig.selfhost").read_text().splitlines()
        self.assertIn(OPTION, lines)

    def test_no_stock_defaults_accept_any_server_host(self) -> None:
        stock = [ROOT / "sdkconfig.defaults", *sorted((ROOT / "devices").glob("sdkconfig.*"))]
        stock = [p for p in stock if p.name != "sdkconfig.selfhost"]
        self.assertGreater(len(stock), 5)
        for path in stock:
            self.assertNotIn(OPTION, path.read_text().splitlines(), path.name)


if __name__ == "__main__":
    unittest.main()
