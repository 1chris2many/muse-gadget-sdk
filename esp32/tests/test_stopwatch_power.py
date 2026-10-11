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

    def test_oled_rail(self):
        """The OLED's rail goes off while paused and its state comes back with it."""
        code = COMMON + defines(self.board, r"LCD_RES|LCD_CHUNK_BYTES|LCD_LEVEL_BOOT|LCD_POWER_UP_MS|LCD_RESET_MS|IOE_\w+|TP_\w+") + r'''
#define MALLOC_CAP_DMA 1
#define MALLOC_CAP_INTERNAL 2
#define heap_caps_calloc(n, size, caps) calloc(n, size)
typedef void *esp_lcd_panel_io_handle_t;
typedef void *esp_lcd_panel_handle_t;
static esp_lcd_panel_io_handle_t s_io = (void *)1;
static esp_lcd_panel_handle_t s_panel = (void *)2;
static void *s_tp = (void *)3;
static char ev[4096];
static uint8_t ioe_out;   /* IO1-8 */
static void note(const char *fmt, int v) { char b[32]; snprintf(b, sizeof(b), fmt, v); strcat(ev, b); strcat(ev, " "); }
static int esp_lcd_panel_io_tx_param(void *io, int cmd, const void *param, size_t n) {
    assert(io == s_io && (cmd >> 24) == 2);
    assert(ioe_out & IOE_L3B);   /* never to an unpowered panel */
    if (n == 1) { char b[32]; snprintf(b, sizeof(b), "c%02x=%d ", (cmd >> 8) & 0xff, *(const uint8_t *)param); strcat(ev, b); }
    else note("c%02x", (cmd >> 8) & 0xff);
    return ESP_OK;
}
static int draws;
static int esp_lcd_panel_reset(void *p) { assert(p == s_panel && (ioe_out & IOE_L3B)); note("swreset", 0); return 0; }
static int esp_lcd_panel_init(void *p) { assert(p == s_panel); note("init", 0); return 0; }
static int esp_lcd_panel_draw_bitmap(void *p, int x1, int y1, int x2, int y2, const void *d) {
    assert(p == s_panel && x1 == 0 && x2 == LCD_RES && y1 % 2 == 0 && y2 % 2 == 0 && y2 > y1 && d);
    for (int i = 0; i < LCD_CHUNK_BYTES; i++) assert(((const uint8_t *)d)[i] == 0);
    if (!draws++) note("black", 0);
    return 0;
}
static int esp_lcd_panel_disp_on_off(void *p, bool on) { assert(p == s_panel && on); note("on", 0); return 0; }
static void muse_lcd_bands_run(void (*fn)(void *), void *arg) { fn(arg); }
static void lcd_bus(void *spi) { note(*(bool *)spi ? "bus" : "lines-low", 0); }
static int ioe_set(uint16_t pins, bool high) {
    note(pins == IOE_L3B ? "l3b=%d" : pins == IOE_LCD_RST ? "rst=%d" : "ioe?=%d", high);
    ioe_out = high ? ioe_out | pins : ioe_out & ~pins; return ESP_OK;
}
static void vTaskDelay(int ms) { note("wait%d", ms); }
static void tp_reset(void) { note("tp-reset", 0); }
static int reg_write(void *dev, uint8_t reg, const uint8_t *v, size_t n) { assert(dev == s_tp && reg == TP_REG_SLEEP && n == 1 && v[0] == 3); note("tp-sleep", 0); return 0; }
static void esp_lv_adapter_pause(int ms) { assert(ms == -1); note("lv-pause", 0); }
static void esp_lv_adapter_resume(void) { note("lv-resume", 0); }
static int esp_lv_adapter_lock(int ms) { (void)ms; return ESP_OK; }
static void esp_lv_adapter_unlock(void) {}
static void *lv_screen_active(void) { return (void *)4; }
static void lv_obj_invalidate(void *o) { assert(o == (void *)4); note("redraw", 0); }
static bool s_lcd_off;
static uint8_t s_lcd_level = LCD_LEVEL_BOOT;
static bool s_lcd_asleep;
''' + "\n".join(function(self.board, n) for n in (
            "lcd_cmd", "panel_setup", "send_brightness", "set_brightness", "send_sleep", "panel_sleep",
            "panel_off", "lcd_power", "display_pause")) + r'''
static void expect(const char *want) {
    if (strcmp(ev, want)) { printf("got:  %s\nwant: %s\n", ev, want); exit(1); }
    ev[0] = 0;
}
int main(int argc, char **argv) {
    assert(argc == 2);
    int test = atoi(argv[1]);
    ioe_out = IOE_L3B | IOE_LCD_RST;
    if (test == 0) {   /* boot: black before display on, M5GFX's brightness */
        panel_setup(NULL);
        expect("swreset init black c51=160 on ");
        assert(draws == (LCD_RES + 7) / 8 && !s_lcd_off);
    } else if (test == 1) {   /* the usual rest: the UI dims and sleeps the panel, then pauses */
        set_brightness(80); panel_sleep(false);
        expect("c51=204 c11 wait120 ");
        set_brightness(0); panel_sleep(true);
        expect("c51=0 c10 wait120 ");
        display_pause(true);
        expect("lv-pause tp-sleep lines-low rst=0 l3b=0 ");
        assert(s_lcd_off && !(ioe_out & (IOE_L3B | IOE_LCD_RST)));
        set_brightness(60);          /* recorded, not sent: the rail is off */
        panel_sleep(false);
        expect("wait120 ");
        set_brightness(0); panel_sleep(true); ev[0] = 0;
        display_pause(false);        /* back as the UI left it: dark and asleep */
        expect("l3b=1 wait20 bus rst=1 wait120 swreset init black c51=0 on c10 wait120 "
               "tp-reset lv-resume redraw ");
        assert(!s_lcd_off);
        panel_sleep(false); set_brightness(80);   /* then the UI wakes it */
        expect("c11 wait120 c51=204 ");
    } else {   /* paused without the UI's sleep (never expected): sleep it before the rail goes */
        set_brightness(50); ev[0] = 0;
        display_pause(true);
        expect("lv-pause tp-sleep c10 wait120 lines-low rst=0 l3b=0 ");
        display_pause(false);
        expect("l3b=1 wait20 bus rst=1 wait120 swreset init black c51=127 on tp-reset lv-resume redraw ");
    }
    return 0;
}
'''
        self.run_cases(self.build("oled", code), 3)

    def test_rest_wiring(self):
        init = function(self.board, "init")
        self.assertIn("muse_gpio_button_init(&s_irq_line, PMIC_IRQ_GPIO)", init)
        self.assertIn("pmic_irq_setup()", init)
        self.assertRegex(self.board, r"#define PMIC_IRQ_GPIO GPIO_NUM_12\b")
        self.assertIn(".wait_buttons = wait_buttons", self.board)
        start = self.board.index("static lv_display_t *display_start(")
        self.assertIn("panel_setup(NULL)", self.board[start:self.board.index("esp_lv_adapter_init", start)])
        self.assertNotIn("0x29", self.board[self.board.index("s_lcd_init[]"):self.board.index("static void lcd_cmd")])


if __name__ == "__main__":
    unittest.main()
