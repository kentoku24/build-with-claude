"""End-to-end test: real quota_push.py fetch driven through the device UI.

Headless, no BLE: runs the REAL `fetch_quota()` from
`buddy/scripts/quota_push.py` (which shells out to `codexbar` on this
machine — both the claude and opencodego providers), feeds the resulting
live heartbeat dict through `BuddyUI.update_heartbeat` (with fake M5/time
modules, the same injection pattern as test_ui_basic_geometry.py), and
asserts the captured `hb-drawn` serial line equals the live dict
recomputed with the SAME `--`-sentinel math as `BuddyUI._print_pct`
(absent key -> "--", else clamped 100-util). Nothing is hardcoded — quota
drifts, so every expected value is derived from the live fetch.

SKIP detection catches the REAL failure mode of a missing/broken codexbar:
`_read_codexbar` raises **SystemExit** (quota_push.py:250-259), NOT
FileNotFoundError/OSError, so fetch_quota() is wrapped in
`except (SystemExit, FileNotFoundError, OSError, ImportError)` and SKIPs
(exit 0) only then. On this machine codexbar is present, so the real path
runs and the test PASSes.

Exits: 0 = PASS (6 bars rendered from live quota); 0 = SKIP (codexbar
genuinely unavailable); 1 = any real mismatch/exception.
"""

import sys
import types
from io import StringIO

# ---- 1. Real quota. quota_push.py imports only stdlib at module level
# (bleak is lazy inside _find_device/_run), so this import is headless-safe.
sys.path.insert(0, "buddy/scripts")
import quota_push

try:
    hb = quota_push.fetch_quota()
except (SystemExit, FileNotFoundError, OSError, ImportError) as e:
    print("SKIP: codexbar/opencodego unavailable: %s" % e)
    sys.exit(0)

# ---- 2. Fake M5/time so the MicroPython UI module imports and runs on
# host Python. Host `time` lacks ticks_ms/ticks_diff (buddy_ui_basic uses
# both in tick_anim); sleep_ms is provided too for safety. Same pattern as
# test_ui_basic_geometry.py:13-64.
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

    def sleep_ms(self, ms):
        return None


sys.modules["M5"] = _FakeM5()
sys.modules["time"] = _FakeTime()

sys.path.insert(0, "buddy/device-basic")
import buddy_ui_basic as ui

LCD = ui._LCD

# ---- 3. Drive the real UI with the real heartbeat. ----
u = ui.BuddyUI()
u.set_connection("encrypted")  # connected steady state -> bars render path
LCD.calls.clear()              # count only update_heartbeat's draw calls
buf = StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
    u.update_heartbeat(hb)
finally:
    sys.stdout = old_stdout
out = buf.getvalue()
labels = [c[1][0] for c in LCD.calls if c[0] == "drawString"]


def fail(msg):
    print("FAIL: %s" % msg)
    sys.exit(1)


# ---- 4. Assertions — every expected value recomputed from the LIVE dict,
# never hardcoded. Mirror BuddyUI._print_pct exactly: absent key -> "--",
# else str(clamped 100 - util). ----
def pct(key):
    v = hb.get(key)
    if v is None:
        return "--"
    return str(max(0, min(100, 100 - int(v))))


expected = ("hb-drawn 5h=%s week=%s bar3=%s go5h=%s gowk=%s gomo=%s "
            "tick5h=%s tickweek=%s\n" % (
                pct("five_h_util"), pct("week_util"), pct("bar3_util"),
                pct("go5h_util"), pct("gowk_util"), pct("gomo_util"),
                "-" if "five_h_expected" not in hb else hb["five_h_expected"],
                "-" if "week_expected" not in hb else hb["week_expected"]))
if out != expected:
    fail("hb-drawn mismatch: got %r want %r" % (out, expected))

# The live OpenCode Go windows must be present with sane int values.
for key in ("go5h_util", "gowk_util", "gomo_util"):
    v = hb.get(key)
    if not isinstance(v, int) or not (0 <= v <= 100):
        fail("%s not an int 0..100 in live hb: %r" % (key, v))

# All six bars actually drawn, each label exactly once (bar3 label
# host-supplied, defaulting the same way the UI does).
bar3_label = hb.get("bar3_label") or "Bar 3"
for lab in ("5h", "Week", bar3_label, "Go5h", "GoWk", "GoMo"):
    if labels.count(lab) != 1:
        fail("label %r drawn %d times (want exactly 1)" % (lab, labels.count(lab)))

print("PASS: 6 bars rendered from live quota")
print("  live hb: %s" % hb)
sys.exit(0)
