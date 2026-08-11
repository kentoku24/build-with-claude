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

import _battery_ip5306 as batt  # noqa: E402  (must import WITHOUT any machine stub)

_SET = {0, 25, 50, 75, 100}
_EXACT_FALLBACK = {"pct": 0, "mV": 0, "mA": 0, "usb": True}

failures = []


def check(name, ok, detail=""):
    if ok:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name}: {detail}")
        failures.append(name)


# a. The 5 canonical bitmasks.
canon = {0x00: 0, 0x80: 25, 0xC0: 50, 0xE0: 75, 0xF0: 100}
bad = {hex(m): (batt.decode_led_bitmap(m), p) for m, p in canon.items()
       if batt.decode_led_bitmap(m) != p}
check("canonical bitmasks -> 0/25/50/75/100", not bad, f"mismatches: {bad}")

# b. Non-canonical 0x90 returns a set value and never raises.
try:
    r = batt.decode_led_bitmap(0x90)
    check("non-canonical 0x90", r in _SET, f"0x90 -> {r}")
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

# d. OSError fallback: inject a fake `machine` whose I2C construction
#    raises OSError, then assert the EXACT fallback dict.  Without the
#    stub, CPython raises ImportError at the inner `import machine`,
#    which `except OSError` never catches -- so the stub is mandatory.
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

if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")
