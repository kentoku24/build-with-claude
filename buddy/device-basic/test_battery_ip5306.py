"""Headless CPython tests for _battery_ip5306 (no device, no MicroPython).

Run: python3 buddy/device-basic/test_battery_ip5306.py
Exits 0 only if every case PASSes; any failure prints the specific
failing value and exits non-zero.
"""

import sys
import types

# The hyphen in `device-basic` blocks a dotted-package import, so add
# the apps dir directly to sys.path before importing.
sys.path.insert(0, "buddy/device-basic/apps")

import _battery_ip5306 as batt  # noqa: E402  (must import WITHOUT any stub)

_SET = {0, 25, 50, 75, 100}
_EXACT_FALLBACK = {"pct": 0, "mV": 0, "mA": 0, "usb": True}

failures = []


def check(name, ok, detail=""):
    if ok:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name}: {detail}")
        failures.append(name)


# a. decode_led_bitmap canonical cases (INVERTED LED semantics per the
#    M5Stack firmware): 0x00->100, 0x80->75, 0xC0->50, 0xE0->25, else->0.
canon = {0x00: 100, 0x80: 75, 0xC0: 50, 0xE0: 25, 0xF0: 0}
bad = {hex(m): (batt.decode_led_bitmap(m), p) for m, p in canon.items()
       if batt.decode_led_bitmap(m) != p}
check("canonical bitmasks -> 100/75/50/25/0", not bad, f"mismatches: {bad}")

# b. Non-canonical 0x90 (leading bits not a canonical run) -> 0.
try:
    r = batt.decode_led_bitmap(0x90)
    check("non-canonical 0x90", r == 0, f"0x90 -> {r}")
except Exception as e:  # pragma: no cover
    check("non-canonical 0x90", False, f"raised {e!r}")

# c. Exhaustive total-function sweep over the whole 256-byte domain.
bad = []
for b in range(256):
    r = batt.decode_led_bitmap(b)
    if r not in _SET:
        bad.append((hex(b), r))
check("exhaustive 0..255 sweep total-function", not bad,
      f"non-set outputs (up to 10): {bad[:10]}")

# d. voltage_to_pct boundaries, mid, and clamping.
vpt = [
    ("boundary low 3300", 3300, 0),
    ("boundary high 4200", 4200, 100),
    ("mid 3750", 3750, 50),
    ("below range clamps to 0", 2000, 0),
    ("above range clamps to 100", 5000, 100),
    ("float input", 3750.0, 50),
]
bad = [(n, batt.voltage_to_pct(mv), want) for n, mv, want in vpt
       if batt.voltage_to_pct(mv) != want]
check("voltage_to_pct boundaries/mid/clamp", not bad, f"got {bad}")

# e. voltage_to_pct totalness: non-numeric input maps to 0, never raises.
bad = []
for junk in (None, "abc", [], object(), -5):
    try:
        r = batt.voltage_to_pct(junk)
        if r != 0:
            bad.append((junk, r))
    except Exception as e:  # pragma: no cover
        bad.append((junk, f"raised {e!r}"))
check("voltage_to_pct total over junk inputs", not bad, f"got {bad}")

# f. OSError fallback: inject a fake `machine` whose I2C construction
#    raises OSError, then assert the EXACT fallback dict.  Without the
#    stub, CPython raises ImportError at the inner `import machine`,
#    which `except OSError` never catches -- so the stub is mandatory.
#    (`import M5` also fails on CPython, which the try/except around the
#    M5 path turns into a pass through to the machine read.)
class _I2C:
    def __init__(self, *args, **kwargs):
        raise OSError("I2C bus absent")

    def readfrom_mem(self, addr, reg, n):
        raise OSError("I2C bus absent")  # pragma: no cover


class _Pin:
    def __init__(self, *args, **kwargs):
        pass


fake_machine = types.ModuleType("machine")
fake_machine.I2C = _I2C
fake_machine.Pin = _Pin
sys.modules["machine"] = fake_machine

try:
    res = batt._read_battery()
    check("OSError fallback EXACT dict", res == _EXACT_FALLBACK,
          f"got {res!r}, want {_EXACT_FALLBACK!r}")
except Exception as e:  # pragma: no cover
    check("OSError fallback EXACT dict", False, f"raised {e!r}")

# g. M5.Power precedence: with a fake `M5` whose Power reports a valid
#    level, _read_battery must return that level (and isCharging as usb)
#    WITHOUT touching the (failing) machine bus.
class _Power:
    def getBatteryLevel(self):
        return 100

    def isCharging(self):
        return True


fake_m5 = types.ModuleType("M5")
fake_m5.Power = _Power()
sys.modules["M5"] = fake_m5

try:
    res = batt._read_battery()
    want = {"pct": 100, "mV": 0, "mA": 0, "usb": True}
    check("M5.Power precedence returns 100", res == want, f"got {res!r}")
except Exception as e:  # pragma: no cover
    check("M5.Power precedence returns 100", False, f"raised {e!r}")

# h. M5.Power absurd value: a level outside 0..100 must NOT be trusted;
#    fall through to the machine read, which raises OSError here -> the
#    exact fallback dict.
class _BadPower(_Power):
    def getBatteryLevel(self):
        return -1


fake_m5.Power = _BadPower()

try:
    res = batt._read_battery()
    check("M5.Power absurd value -> fallback", res == _EXACT_FALLBACK,
          f"got {res!r}")
except Exception as e:  # pragma: no cover
    check("M5.Power absurd value -> fallback", False, f"raised {e!r}")

# Drop the stubs so nothing leaks to other tests in this process.
del sys.modules["M5"]
del sys.modules["machine"]

if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")
