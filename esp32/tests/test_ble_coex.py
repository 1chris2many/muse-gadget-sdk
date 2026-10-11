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
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BleCoexTest(unittest.TestCase):
    def _run(self, *defines: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "ble_coex"
            built = subprocess.run(
                [*shlex.split(os.environ.get("CC", "cc")), "-std=c11", "-O1",
                 "-Wall", "-Wextra", "-Werror",
                 "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                 *[f"-D{d}" for d in defines],
                 "-I", str(ROOT / "tests" / "ble_coex_fakes"),
                 "-I", str(ROOT / "main"),
                 str(ROOT / "tests" / "ble_coex_harness.c"),
                 str(ROOT / "main" / "ble_coex.c"),
                 "-o", str(binary)],
                capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("ok", ran.stdout)

    def test_withholds_advertising_only(self) -> None:
        self._run()

    def test_withholds_advertising_and_connections(self) -> None:
        self._run("TEST_CONNECTED")

    def test_wired_into_the_build_and_app(self) -> None:
        cmake = (ROOT / "main" / "CMakeLists.txt").read_text()
        self.assertIn("-Wl,--wrap=coex_schm_status_bit_set", cmake)
        self.assertIn("-Wl,--wrap=coex_schm_status_bit_clear", cmake)
        app = (ROOT / "main" / "app.c").read_text()
        # On once setup completes, off whenever setup advertising starts.
        self.assertGreaterEqual(app.count("ble_coex_set_active(true)"), 3)
        self.assertGreaterEqual(app.count("ble_coex_set_active(false)"), 2)
        kconfig = (ROOT / "main" / "Kconfig.projbuild").read_text()
        self.assertTrue(re.search(r"config HOMEHUB_BLE_COEX\n.*\n\s+depends on HOMEHUB_BLE_PERSISTENT", kconfig))


if __name__ == "__main__":
    unittest.main()
