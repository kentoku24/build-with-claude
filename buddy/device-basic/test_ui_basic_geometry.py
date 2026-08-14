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


def check_column_bounds(phase, x_lo, x_hi):
    """Column-scoped bounds for the data-rows phase (y in 28..121): any draw
    whose x falls in [x_lo, x_hi) must not cross x_hi.

    This catches a left-column pct label painted over the right column — the
    full-screen check_bounds can't. Scoping to the bar rows (y 28..121)
    keeps screen-wide draws out of the way: the header icon (~x=300 at y=5)
    and the battery fill (x=247 at y=183) would otherwise false-flag.
    """
    bad = []
    for name, args in LCD.calls:
        if name in ("fillRect", "drawRect"):
            x, y, w, _ = args[0], args[1], args[2], args[3]
        elif name == "drawString":
            s, x, y = args[0], args[1], args[2]
            w = LCD.textWidth(s)
        else:
            continue
        if not (x_lo <= x < x_hi):
            continue
        if not (28 <= y <= 121):
            continue
        if x + w > x_hi:
            bad.append("%s%s" % (name, args))
            print("FAIL %s: column [%d,%d) overrun %s%s"
                  % (phase, x_lo, x_hi, name, args))
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

# ---- phase d: Go-absent heartbeat + exact hb-drawn + left marker ----
clear_calls()
buf = StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
    u.update_heartbeat({
        "five_h_util": 13,
        "five_h_expected": 27, "five_h_expected_color": 0x00FF00,
        "week_util": 13,
        "bar3_util": 6, "bar3_label": "Routines",
    })
finally:
    sys.stdout = old_stdout
out = buf.getvalue()
exp = ("hb-drawn 5h=87 week=87 bar3=94 go5h=-- gowk=-- gomo=-- "
       "tick5h=27 tickweek=-\n")
check(out == exp, "hb-drawn mismatch: got %r want %r" % (out, exp))
marks = [c for c in LCD.calls if c[0] == "fillRect" and c[1][2] == 4]
check(len(marks) == 1, "expected exactly one 4px marker, got %d" % len(marks))
mx = marks[0][1][0] + 1
check(mx == 115, "go-absent marker mx=%d != 115" % mx)
strings = [c[1][0] for c in LCD.calls if c[0] == "drawString"]
for lab in ("5h", "Week", "Routines", "Go5h", "GoWk", "GoMo"):
    check(lab in strings, "go-absent phase missing label %r" % lab)
check_bounds("go-absent")
check_column_bounds("go-absent", 6, 156)
check_column_bounds("go-absent", 164, 314)
print("PASS go-absent 6-bar")

# ---- phase e: full 6-field heartbeat + exact hb-drawn ----
clear_calls()
buf = StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
    u.update_heartbeat({
        "five_h_util": 13,
        "five_h_expected": 27, "five_h_expected_color": 0x00FF00,
        "week_util": 13,
        "bar3_util": 6, "bar3_label": "Routines",
        "go5h_util": 10, "go5h_color": 0x00FF00,
        "gowk_util": 16, "gowk_color": 0xFFAA00,
        "gomo_util": 5, "gomo_color": 0x00FF00,
    })
finally:
    sys.stdout = old_stdout
out = buf.getvalue()
exp = ("hb-drawn 5h=87 week=87 bar3=94 go5h=90 gowk=84 gomo=95 "
       "tick5h=27 tickweek=-\n")
check(out == exp, "hb-drawn mismatch: got %r want %r" % (out, exp))
marks = [c for c in LCD.calls if c[0] == "fillRect" and c[1][2] == 4]
check(len(marks) == 1, "expected exactly one 4px marker, got %d" % len(marks))
mx = marks[0][1][0] + 1
check(mx == 115, "full marker mx=%d != 115" % mx)
check_bounds("full-6-field")
check_column_bounds("full-6-field", 6, 156)
check_column_bounds("full-6-field", 164, 314)
print("PASS full 6-field")

# ---- phase f: prompt pending hides BOTH 3rd-row bars ----
clear_calls()
buf = StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
    u.update_heartbeat({
        "five_h_util": 13,
        "week_util": 13,
        "bar3_util": 6, "bar3_label": "Routines",
        "go5h_util": 10, "gowk_util": 16, "gomo_util": 5,
        "prompt": {"tool": "Bash", "hint": "Allow this?"},
    })
finally:
    sys.stdout = old_stdout
out = buf.getvalue()
exp = ("hb-drawn 5h=87 week=87 bar3=94 go5h=90 gowk=84 gomo=95 "
       "tick5h=- tickweek=-\n")
check(out == exp, "hb-drawn mismatch: got %r want %r" % (out, exp))
strings = [c[1][0] for c in LCD.calls if c[0] == "drawString"]
check("Go5h" in strings and "GoWk" in strings,
      "prompt phase lost Go5h/GoWk")
check("Routines" not in strings,
      "prompt phase still drew the bar3 label")
check("GoMo" not in strings,
      "prompt phase still drew GoMo")
check(("drawRect", (3, 104, _W - 6, 64, ui.ORANGE)) in LCD.calls,
      "prompt box missing")
check_bounds("prompt")
check_column_bounds("prompt", 6, 156)
check_column_bounds("prompt", 164, 314)
print("PASS prompt-hidden")

# ---- phase g: footer BAT bar ----
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
