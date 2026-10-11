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

"""Compile the StopWatch's battery-rest power code against host fakes."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MUSE = ROOT / "components/muse"


def function(source, name):
    match = re.search(r"^(?:static )?(?:unsigned|int|void|esp_err_t|bool) " + name + r"\([^)]*\)\n\{", source, re.M)
    start = match.start()
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def defines(source, pattern):
    return "\n".join(re.findall(r"^#define (?:" + pattern + r")\b.*", source, re.M))


COMMON = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define BIT(n) (1u << (n))
#define ESP_OK 0
#define ESP_FAIL -1
#define pdMS_TO_TICKS(n) (n)
#define TAG "test"
typedef int esp_err_t;
#define ESP_RETURN_ON_ERROR(expr, tag, ...) do { int e_ = (expr); if (e_) return e_; } while (0)
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
'''


class StopWatchPower(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.board = (MUSE / "boards/board_m5stack_stopwatch.c").read_text()
        cls.flags = defines((MUSE / "muse_board.h").read_text(), r"MUSE_BTN_\w+")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, name, code):
        path = Path(self.tmp.name) / (name + ".c")
        path.write_text(code)
        cc = shlex.split(os.environ.get("CC", "cc"))
        result = subprocess.run(cc + ["-std=gnu11", "-Wall", "-Wextra", "-Werror", str(path), "-o", str(path.with_suffix(""))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return path.with_suffix("")

    def run_cases(self, binary, cases):
        for case in range(cases):
            with self.subTest(case=case):
                result = subprocess.run([str(binary), str(case)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_power_key_interrupt(self):
        """PM1 IRQ setup, click to tap, credit for a seen press, stuck line and paused waits."""
        code = COMMON + self.flags + "\n" + defines(self.board, r"PMIC_\w+|POWER_\w+|CLICK_\w+|REST_\w+|IRQ_\w+") + r'''
#define GPIO_NUM_12 12
static uint8_t regs[256];
static int fail_reg = -1, stuck, writes_to_status;
static uint8_t key;
static int64_t now = 10000000;
static void *s_pmic;
static int line(void) { return !(stuck || (regs[0x42] & ~regs[0x45] & 7)); }
static int gpio_get_level(int gpio) { assert(gpio == 12); return line(); }
static int reg_read(void *dev, uint8_t reg, uint8_t *out, size_t n) {
    (void)dev; assert(n == 1);
    if (reg == fail_reg) return -7;
    *out = reg == 0x48 ? key : regs[reg]; return ESP_OK;
}
static int reg_write(void *dev, uint8_t reg, const uint8_t *data, size_t n) {
    (void)dev; assert(n == 1);
    if (reg == fail_reg) return -7;
    if (reg >= 0x40 && reg <= 0x42) { assert(data[0] == 0); ++writes_to_status; }
    regs[reg] = data[0]; return ESP_OK;
}
static int64_t esp_timer_get_time(void) { return now; }
static int delayed, waited_n, waited_ms;
static void vTaskDelay(int ms) { delayed = ms; }
typedef struct { int gpio; } muse_gpio_button_t;
static muse_gpio_button_t s_talk = { 1 }, s_speaker = { 2 }, s_irq_line = { 12 };
static void muse_gpio_buttons_wait(muse_gpio_button_t *const *b, int n, int ms) {
    assert(b[0] == &s_talk && b[1] == &s_speaker && (n == 2 || b[2] == &s_irq_line));
    waited_n = n; waited_ms = ms;
}
static bool s_power_pressed, s_power_candidate, s_power_armed = true, s_power_long;
static int64_t s_power_changed_at, s_power_held_at, s_power_released_at;
static bool s_pmic_irq = true, s_irq_stuck;
''' + "\n".join(function(self.board, n) for n in (
            "pmic_update", "pmic_irq_clear", "pmic_irq_setup", "take_power_irq", "poll_power_button", "wait_buttons")) + r'''
static unsigned sample(uint8_t k, int us) { key = k; now += us; return poll_power_button(); }
static void reset_wait(void) { delayed = waited_n = waited_ms = -1; }
int main(int argc, char **argv) {
    assert(argc == 2);
    int test = atoi(argv[1]);
    if (test == 0) {   /* setup: only the clicks interrupt, 125 ms click delay, GPIO1 as IRQ, statuses cleared */
        regs[0x49] = 0x1F; regs[0x42] = 0x03; regs[0x40] = 0x04; regs[0x16] = 0x00; regs[0x13] = 0x00;
        assert(pmic_irq_setup() == ESP_OK);
        assert(regs[0x43] == 0x1F && regs[0x44] == 0x3F && regs[0x45] == 0x02);
        assert(regs[0x49] == 0x19);              /* click delay 125 ms; reset-disable and long delay kept */
        assert(regs[0x13] == 0x02 && regs[0x16] == 0x04);
        assert(regs[0x40] == 0 && regs[0x41] == 0 && regs[0x42] == 0 && line());
        fail_reg = 0x45; assert(pmic_irq_setup() == -7);
    } else if (test == 1) {   /* a tap at rest arrives as a click once released: press + release */
        regs[0x42] = 0x01; assert(!line());
        assert(sample(0, 10000) == (MUSE_BTN_POWER_PRESS | MUSE_BTN_POWER_RELEASE));
        assert(line() && regs[0x42] == 0 && writes_to_status == 3 && !s_irq_stuck);
        assert(sample(0, 10000) == 0);           /* once */
    } else if (test == 2) {   /* the poll saw the press: its click adds nothing */
        assert(sample(1, 10000) == 0); assert(sample(1, 30000) == MUSE_BTN_POWER_PRESS);
        assert(sample(0, 10000) == 0); assert(sample(0, 30000) == MUSE_BTN_POWER_RELEASE);
        regs[0x42] = 0x01; assert(sample(0, 125000) == 0 && line());
        regs[0x42] = 0x01; assert(sample(0, 1500000) == (MUSE_BTN_POWER_PRESS | MUSE_BTN_POWER_RELEASE));
    } else if (test == 3) {   /* key down again, not armed, or wake-only events: no fabricated tap */
        regs[0x42] = 0x01; assert(sample(1, 10000) == 0);
        assert(sample(1, 30000) == MUSE_BTN_POWER_PRESS);
        assert(sample(0, 10000) == 0); assert(sample(0, 30000) == MUSE_BTN_POWER_RELEASE);
        now += 2000000; s_power_armed = false;
        regs[0x42] = 0x01; assert(sample(0, 10000) == 0 && line());
        s_power_armed = true;
        regs[0x42] = 0x02; regs[0x45] = 0x00; assert(sample(0, 10000) == 0 && line()); /* WAKEUP only */
    } else if (test == 4) {   /* a failed key read keeps the click for the next poll */
        regs[0x42] = 0x01; fail_reg = 0x48;
        assert(sample(0, 10000) == 0 && line());
        fail_reg = -1; assert(sample(0, 10000) == (MUSE_BTN_POWER_PRESS | MUSE_BTN_POWER_RELEASE));
    } else if (test == 5) {   /* paused waits */
        reset_wait(); wait_buttons(1000); assert(waited_n == 3 && waited_ms == 1000 && delayed == -1);
        reset_wait(); wait_buttons(10000); assert(waited_n == 3 && waited_ms == 10000);
        regs[0x42] = 0x01; reset_wait(); wait_buttons(1000);
        assert(waited_n == -1 && delayed == 10);   /* fell since the poll: take it first */
        regs[0x42] = 0;
        s_power_candidate = true; reset_wait(); wait_buttons(1000);
        assert(waited_n == -1 && delayed == 50);   /* settling: polled as before */
        s_power_candidate = false; s_power_pressed = true; reset_wait(); wait_buttons(1000);
        assert(waited_n == -1 && delayed == 50);   /* held: polled for the 1.5 s hold */
        s_power_pressed = false; s_pmic_irq = false; reset_wait(); wait_buttons(1000);
        assert(waited_n == 2 && waited_ms == 200); /* no interrupt: capped */
        reset_wait(); wait_buttons(150); assert(waited_n == 2 && waited_ms == 150);
    } else {   /* a line that won't clear is distrusted until it reads high again */
        stuck = 1; assert(sample(0, 10000) == 0 && s_irq_stuck);
        reset_wait(); wait_buttons(1000); assert(waited_n == 2 && waited_ms == 200);
        stuck = 0; assert(sample(0, 10000) == 0 && !s_irq_stuck);
        reset_wait(); wait_buttons(1000); assert(waited_n == 3 && waited_ms == 1000);
    }
    return 0;
}
'''
        self.run_cases(self.build("irq", code), 7)

    def test_rest_wiring(self):
        init = function(self.board, "init")
        self.assertIn("muse_gpio_button_init(&s_irq_line, PMIC_IRQ_GPIO)", init)
        self.assertIn("pmic_irq_setup()", init)
        self.assertRegex(self.board, r"#define PMIC_IRQ_GPIO GPIO_NUM_12\b")
        self.assertIn(".wait_buttons = wait_buttons", self.board)


if __name__ == "__main__":
    unittest.main()
