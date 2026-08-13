"""Headless geometry test for buddy_ui_basic (320x240). No device required.

Injects fake M5/time modules before importing buddy_ui_basic, drives the
public BuddyUI surface, and checks every draw call stays in-bounds plus the
exact heartbeat printout. Exits 0 only if every phase passes.
"""

import sys
import types
from io import StringIO


class _FakeLCD:
    def __init__(self):
        self.calls = []
        self.FONTS = types.SimpleNamespace(
            DejaVu9="DejaVu9", EFontJA24="EFontJA24"
        )

    def _rec(self, name, args):
        self.calls.append((name, tuple(args)))

    def fillRect(self, x, y, w, h, c):
        self._rec("fillRect", (x, y, w, h, c))

    def drawRect(self, x, y, w, h, c):
        self._rec("drawRect", (x, y, w, h, c))

    def drawString(self, s, x, y):
        self._rec("drawString", (s, x, y))

    def setFont(self, f):
        self._rec("setFont", (f,))

    def setTextSize(self, s):
        self._rec("setTextSize", (s,))

    def setTextColor(self, fg, bg):
        self._rec("setTextColor", (fg, bg))

    def fillScreen(self, c):
        self._rec("fillScreen", (c,))

    def textWidth(self, text):
        return 6 * len(text)


class _FakeM5:
    Lcd = _FakeLCD()


class _FakeTime:
    def ticks_ms(self):
        return 0

    def ticks_diff(self, a, b):
        return a - b


sys.modules["M5"] = _FakeM5()
sys.modules["time"] = _FakeTime()

sys.path.insert(0, "buddy/device-basic")
import buddy_ui_basic as ui

LCD = ui._LCD
_W = ui._W
_H = ui._H

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print("FAIL", msg)


def clear_calls():
    LCD.calls.clear()


def check_bounds(phase):
    bad = []
    for name, args in LCD.calls:
        if name in ("fillRect", "drawRect"):
            x, y, w, h = args[0], args[1], args[2], args[3]
            if not (0 <= x and x + w <= _W and 0 <= y and y + h <= _H):
                bad.append("%s%s" % (name, args))
                print("FAIL %s: out-of-bounds %s%s" % (phase, name, args))
        elif name == "drawString":
            s, x, y = args[0], args[1], args[2]
            tw = LCD.textWidth(s)
            if not (x >= 0 and x + tw <= _W):
                bad.append("drawString %s" % (args,))
                print("FAIL %s: out-of-bounds drawString %s" % (phase, args))
    for b in bad:
        failures.append(b)
    return not bad


# ---- phase a: surface + tick_idle_burst ----
clear_calls()
u = ui.BuddyUI()
clear_calls()
required = [
    "set_connection", "show_passkey", "clear_passkey", "show_unpair_prompt",
    "clear_unpair_prompt", "update_heartbeat", "update_identity",
    "update_footer", "flash_decision", "flash_toast", "restore_button_hints",
    "is_idle", "tick_idle_burst", "tick_anim",
]
missing = [m for m in required if not hasattr(ui.BuddyUI, m)]
check(not missing, "missing methods: %s" % missing)
check(u.tick_idle_burst(5, 9) == (5, 9), "tick_idle_burst(5,9) != (5,9)")
print("PASS surface")

# ---- phase b: overlays geometry ----
clear_calls()
u.show_passkey(123456)
u.clear_passkey()
u.show_unpair_prompt()
u.clear_unpair_prompt()
check_bounds("overlays")
print("PASS overlays")

# ---- phase c: set_connection ----
clear_calls()
u.set_connection("encrypted")
check_bounds("set_connection")
print("PASS set_connection")

# ---- phase d: reserve marker case + exact hb-drawn ----
clear_calls()
buf = StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
    u.update_heartbeat({
        "five_h_util": 13, "five_h_color": 0x00FF00,
        "five_h_expected": 27, "five_h_expected_color": 0x00FF00,
        "week_util": 13, "week_color": 0x00FF00,
        "sonnet_util": 6, "sonnet_color": 0x00FF00,
    })
finally:
    sys.stdout = old_stdout
out = buf.getvalue()
exp = "hb-drawn 5h=87 week=87 sonnet=94 tick5h=27 tickweek=-\n"
check(out == exp, "hb-drawn mismatch: got %r want %r" % (out, exp))
marks = [c for c in LCD.calls if c[0] == "fillRect" and c[1][2] == 4]
check(len(marks) == 1, "expected exactly one 4px marker, got %d" % len(marks))
mx = marks[0][1][0] + 1
check(mx == 230, "reserve marker mx=%d != 230" % mx)
check_bounds("reserve")
print("PASS reserve marker")

# ---- phase e: deficit marker case + exact hb-drawn ----
clear_calls()
buf = StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
    u.update_heartbeat({
        "five_h_util": 31, "five_h_color": 0xFFFF00,
        "five_h_expected": 27, "five_h_expected_color": 0x00FF00,
        "week_util": 13, "week_color": 0x00FF00,
        "sonnet_util": 6, "sonnet_color": 0x00FF00,
    })
finally:
    sys.stdout = old_stdout
out = buf.getvalue()
exp = "hb-drawn 5h=69 week=87 sonnet=94 tick5h=27 tickweek=-\n"
check(out == exp, "hb-drawn mismatch: got %r want %r" % (out, exp))
marks = [c for c in LCD.calls if c[0] == "fillRect" and c[1][2] == 4]
check(len(marks) == 1, "expected exactly one 4px marker, got %d" % len(marks))
mx = marks[0][1][0] + 1
check(mx == 230, "deficit marker mx=%d != 230" % mx)
check_bounds("deficit")
print("PASS deficit marker")

# ---- phase f: footer BAT bar ----
clear_calls()
u.update_footer({"lvl": 1, "appr": 2, "deny": 0},
                {"pct": 10, "mV": 0, "mA": 0, "usb": True})
red = ("fillRect", (247, 183, 3, 8, 0xFF0000))
check(red in LCD.calls, "missing RED bat fill %s" % (red,))
check_bounds("footer red")
clear_calls()
u.update_footer({"lvl": 1, "appr": 2, "deny": 0},
                {"pct": 50, "mV": 0, "mA": 0, "usb": True})
green = ("fillRect", (247, 183, 18, 8, 0x00FF00))
check(green in LCD.calls, "missing GREEN bat fill %s" % (green,))
check_bounds("footer green")
print("PASS footer battery")

if failures:
    print("FAIL: %d check(s) failed" % len(failures))
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
